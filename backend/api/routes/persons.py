"""VIP API — Persons routes."""

from __future__ import annotations

import uuid as _uuid
from typing import Optional

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database.db import get_db
from backend.database.models import Person
from backend.pipeline.centroid import update_person_centroid

# Minimum cosine similarity to surface a merge suggestion
_SUGGEST_THRESHOLD = 0.50

router = APIRouter()


class NamePersonRequest(BaseModel):
    name: str


class MergeRequest(BaseModel):
    into_person_id: int     # merge source → target (kept for backward compat)


class MergeNamedPersonsRequest(BaseModel):
    new_name: Optional[str] = None   # None = keep the survivor's current name


class FindSimilarAllRequest(BaseModel):
    auto_threshold: float = 0.85   # Clusters >= this similarity are merged automatically


@router.get("")
async def list_persons():
    """All persons — named and unnamed (clusters awaiting a name)."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT p.id, p.uuid, p.name, p.created_at, p.named_at,
                   p.is_merged, p.merged_into_id,
                   COUNT(DISTINCT f.media_file_id)            AS photo_count,
                   (SELECT COUNT(*) FROM persons p2
                    WHERE p2.merged_into_id = p.id
                      AND p2.is_merged = 1)                   AS merge_sources_count,
                   MIN(f.thumbnail_path)                      AS representative_thumbnail,
                   CASE WHEN p.name IS NOT NULL AND EXISTS (
                       SELECT 1 FROM writeback_queue wq
                       JOIN faces f2 ON f2.media_file_id = wq.media_file_id
                       WHERE f2.person_id = p.id AND wq.status = 'written'
                   ) THEN 1 ELSE 0 END                        AS name_written
            FROM persons p
            LEFT JOIN faces f ON f.person_id = p.id
            WHERE p.is_merged = 0 AND p.is_ignored = 0
            GROUP BY p.id
            ORDER BY photo_count DESC
        """)
    return [dict(r) for r in rows]


@router.get("/unnamed")
async def list_unnamed_clusters():
    """Clusters not yet assigned to a named (non-ignored) person."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT c.id, c.member_count, c.intra_similarity, c.is_high_conf,
                   MIN(f.thumbnail_path) as representative_thumbnail
            FROM clusters c
            LEFT JOIN faces f ON f.cluster_id = c.id AND f.thumbnail_path IS NOT NULL
            WHERE c.person_id IS NULL
            GROUP BY c.id
            ORDER BY c.member_count DESC
        """)
    return [dict(r) for r in rows]


@router.get("/clusters/{cluster_id}/similar")
async def get_similar_clusters(cluster_id: int, limit: int = 8):
    """
    Return unnamed clusters visually similar to the given cluster,
    ranked by cosine similarity between stored centroids.

    Used in the dismiss modal to help the user understand which other
    face tiles would also be affected by an ‘always ignore’ decision.
    """
    async with get_db() as db:
        source = await (
            await db.execute(
                "SELECT centroid FROM clusters WHERE id=? AND person_id IS NULL",
                (cluster_id,),
            )
        ).fetchone()
        if not source or not source["centroid"]:
            return []

        source_vec = np.frombuffer(source["centroid"], dtype=np.float32).copy()
        norm = np.linalg.norm(source_vec)
        if norm > 0:
            source_vec /= norm

        candidates = await db.execute_fetchall("""
            SELECT c.id AS cluster_id, c.member_count, c.intra_similarity,
                   c.is_high_conf, c.centroid,
                   MIN(f.thumbnail_path) AS representative_thumbnail
            FROM clusters c
            LEFT JOIN faces f ON f.cluster_id = c.id AND f.thumbnail_path IS NOT NULL
            WHERE c.person_id IS NULL AND c.id != ?
            GROUP BY c.id
        """, (cluster_id,))

    scored = []
    for cand in candidates:
        if not cand["centroid"]:
            continue
        vec = np.frombuffer(cand["centroid"], dtype=np.float32).copy()
        n = np.linalg.norm(vec)
        if n > 0:
            vec /= n
        sim = float(np.dot(source_vec, vec))
        scored.append({**{k: cand[k] for k in cand.keys() if k != "centroid"}, "similarity": round(sim, 3)})

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:limit]


@router.delete("/clusters/{cluster_id}")
async def delete_cluster(cluster_id: int):
    """
    Delete an unnamed cluster.  All faces in the cluster return to the
    unassigned pool and will be re-evaluated on the next pipeline run.
    """
    async with get_db() as db:
        row = await (
            await db.execute("SELECT person_id FROM clusters WHERE id=?", (cluster_id,))
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Cluster not found")
        if row["person_id"] is not None:
            # Allow deleting attached-to-ignored-person clusters; block named-person clusters.
            p = await (
                await db.execute(
                    "SELECT is_ignored FROM persons WHERE id=?", (row["person_id"],)
                )
            ).fetchone()
            if not p or not p["is_ignored"]:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot delete a cluster that belongs to a named person.",
                )
            # Detach faces from the ignored person too
            await db.execute(
                "UPDATE faces SET person_id=NULL WHERE cluster_id=?", (cluster_id,)
            )
            # Clean up the ignored person record if it has no remaining clusters
            remaining = await (
                await db.execute(
                    "SELECT COUNT(*) AS n FROM clusters WHERE person_id=? AND id != ?",
                    (row["person_id"], cluster_id),
                )
            ).fetchone()
            if remaining and remaining["n"] == 0:
                await db.execute(
                    "DELETE FROM persons WHERE id=? AND is_ignored=1",
                    (row["person_id"],),
                )
        # Release face → cluster assignments
        await db.execute("UPDATE faces SET cluster_id=NULL WHERE cluster_id=?", (cluster_id,))
        await db.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))
    return {"status": "deleted", "cluster_id": cluster_id}


@router.post("/clusters/{cluster_id}/ignore")
async def ignore_cluster(cluster_id: int):
    """
    Mark an unnamed cluster as ‘always ignore’.

    Creates a hidden person record (is_ignored=1) and assigns the cluster to
    it.  During future pipeline runs, face detections whose ArcFace centroid
    is close enough to this person’s centroid are automatically suppressed
    without surfacing in the unnamed clusters list or suggestion cards.
    """
    async with get_db() as db:
        row = await (
            await db.execute(
                "SELECT id FROM clusters WHERE id=? AND person_id IS NULL", (cluster_id,)
            )
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404, detail="Cluster not found or already assigned to a person."
            )

        new_uuid = str(_uuid.uuid4())
        cursor = await db.execute(
            "INSERT INTO persons (uuid, is_ignored) VALUES (?, 1)", (new_uuid,)
        )
        person_id = cursor.lastrowid

        await db.execute(
            "UPDATE clusters SET person_id=? WHERE id=?", (person_id, cluster_id)
        )
        await db.execute(
            "UPDATE faces SET person_id=? WHERE cluster_id=?", (person_id, cluster_id)
        )

        # Persist centroid so Phase 3b can suppress re-detections in future scans
        await update_person_centroid(db, person_id)

    return {"status": "ignored", "cluster_id": cluster_id, "person_id": person_id}


@router.patch("/{person_id}/name")
async def name_person(person_id: int, req: NamePersonRequest):
    """Assign or update the name of a person."""
    async with get_db() as db:
        existing = await (
            await db.execute("SELECT id FROM persons WHERE id=?", (person_id,))
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Person not found")

        await db.execute("""
            UPDATE persons SET name=?, named_at=datetime('now') WHERE id=?
        """, (req.name, person_id))

        # Queue all this person's photos for writeback.
        # INSERT OR REPLACE resets any existing row back to 'pending' so that
        # renaming a person after writeback causes their files to be re-written.
        await db.execute("""
            INSERT OR REPLACE INTO writeback_queue (media_file_id)
            SELECT DISTINCT f.media_file_id
            FROM faces f
            WHERE f.person_id = ?
        """, (person_id,))

        # Persist centroid so this person is recognisable in future scans
        await update_person_centroid(db, person_id)

    return {"status": "ok", "name": req.name}


@router.post("/merge")
async def merge_persons(req: MergeRequest, source_id: int):
    """Merge two persons (legacy endpoint — use /{a}/merge-with/{b} instead)."""
    async with get_db() as db:
        await db.execute(
            "UPDATE faces SET person_id=? WHERE person_id=?",
            (req.into_person_id, source_id),
        )
        await db.execute("""
            UPDATE persons SET is_merged=1, merged_into_id=? WHERE id=?
        """, (req.into_person_id, source_id))
        await _sync_photo_count(db, req.into_person_id)
        await update_person_centroid(db, req.into_person_id)

    return {"status": "merged", "into": req.into_person_id}


@router.post("/{person_a_id}/merge-with/{person_b_id}")
async def merge_named_persons(
    person_a_id: int,
    person_b_id: int,
    req: MergeNamedPersonsRequest,
):
    """
    Merge two named persons into a single record end-to-end.

    Survivor is determined by photo count (ties favour person_a).
    All faces and clusters from the loser are moved to the survivor.
    The old name is removed from photo files on the next writeback run.

    Body:
        new_name: optional override for the merged person's name.
                  Omit to keep the survivor's existing name.
    """
    if person_a_id == person_b_id:
        raise HTTPException(status_code=400, detail="Cannot merge a person with themselves.")

    async with get_db() as db:
        row_a = await (
            await db.execute(
                "SELECT id, name, is_merged, is_ignored FROM persons WHERE id=?",
                (person_a_id,),
            )
        ).fetchone()
        row_b = await (
            await db.execute(
                "SELECT id, name, is_merged, is_ignored FROM persons WHERE id=?",
                (person_b_id,),
            )
        ).fetchone()

        if not row_a:
            raise HTTPException(status_code=404, detail=f"Person {person_a_id} not found.")
        if not row_b:
            raise HTTPException(status_code=404, detail=f"Person {person_b_id} not found.")
        if row_a["is_merged"]:
            raise HTTPException(status_code=400, detail=f"Person {person_a_id} is already merged.")
        if row_b["is_merged"]:
            raise HTTPException(status_code=400, detail=f"Person {person_b_id} is already merged.")

        # Determine survivor = person with more associated photos; ties → person_a.
        count_a_row = await (
            await db.execute(
                "SELECT COUNT(DISTINCT media_file_id) AS n FROM faces WHERE person_id=?",
                (person_a_id,),
            )
        ).fetchone()
        count_b_row = await (
            await db.execute(
                "SELECT COUNT(DISTINCT media_file_id) AS n FROM faces WHERE person_id=?",
                (person_b_id,),
            )
        ).fetchone()

        count_a = count_a_row["n"] if count_a_row else 0
        count_b = count_b_row["n"] if count_b_row else 0

        if count_b > count_a:
            survivor_id, loser_id = person_b_id, person_a_id
            survivor_name = row_b["name"]
        else:
            survivor_id, loser_id = person_a_id, person_b_id
            survivor_name = row_a["name"]

        new_name_clean = req.new_name.strip() if req.new_name and req.new_name.strip() else None
        effective_name = new_name_clean if new_name_clean else survivor_name

        # ── All DB mutations in a single implicit transaction ───────────────
        # 1. Reassign all faces from loser → survivor.
        await db.execute(
            "UPDATE faces SET person_id=? WHERE person_id=?",
            (survivor_id, loser_id),
        )
        # 2. Reassign all clusters from loser → survivor.
        await db.execute(
            "UPDATE clusters SET person_id=? WHERE person_id=?",
            (survivor_id, loser_id),
        )
        # 3. Apply the effective name to the survivor (update named_at if changed).
        if effective_name != survivor_name or not survivor_name:
            await db.execute(
                "UPDATE persons SET name=?, named_at=datetime('now') WHERE id=?",
                (effective_name, survivor_id),
            )
        # 4. Mark loser as merged (hidden from all future queries).
        await db.execute(
            "UPDATE persons SET is_merged=1, merged_into_id=? WHERE id=?",
            (survivor_id, loser_id),
        )
        # 5. Clear merge-suggestion rejections for both.
        await db.execute(
            "DELETE FROM rejected_suggestions WHERE person_id IN (?, ?)",
            (survivor_id, loser_id),
        )
        # 6. Queue ALL survivor photos (post-reassignment) for writeback.
        #    INSERT OR REPLACE resets any prior 'written' row back to 'pending'
        #    so ExifTool will overwrite files, removing the loser's old name and
        #    writing the effective_name in its place.
        await db.execute("""
            INSERT OR REPLACE INTO writeback_queue (media_file_id)
            SELECT DISTINCT media_file_id FROM faces WHERE person_id=?
        """, (survivor_id,))

        # Count the queued photos for the response summary.
        queued_row = await (
            await db.execute(
                "SELECT COUNT(DISTINCT media_file_id) AS n FROM faces WHERE person_id=?",
                (survivor_id,),
            )
        ).fetchone()
        photos_queued = queued_row["n"] if queued_row else 0

        # 7. Refresh the denormalised photo_count column.
        await _sync_photo_count(db, survivor_id)

        # 8. Recompute centroid from all merged embeddings.
        await update_person_centroid(db, survivor_id)

    return {
        "status": "merged",
        "survivor_id": survivor_id,
        "survivor_name": effective_name,
        "absorbed_id": loser_id,
        "photos_queued_for_writeback": photos_queued,
    }


class NameClusterRequest(BaseModel):
    name: str


@router.post("/from-cluster/{cluster_id}")
async def create_person_from_cluster(cluster_id: int, req: NameClusterRequest):
    """Create a named person from a cluster in one step."""
    async with get_db() as db:
        person_uuid = str(_uuid.uuid4())
        cursor = await db.execute("""
            INSERT INTO persons (uuid, name, named_at) VALUES (?, ?, datetime('now'))
        """, (person_uuid, req.name.strip()))
        person_id = cursor.lastrowid

        await db.execute(
            "UPDATE clusters SET person_id=? WHERE id=?", (person_id, cluster_id)
        )
        await db.execute(
            "UPDATE faces SET person_id=? WHERE cluster_id=?", (person_id, cluster_id)
        )
        await _sync_photo_count(db, person_id)

        # Queue photos for writeback
        await db.execute("""
            INSERT OR REPLACE INTO writeback_queue (media_file_id)
            SELECT DISTINCT media_file_id FROM faces WHERE cluster_id=?
        """, (cluster_id,))

        # Persist centroid — this person now has embeddings to match against
        await update_person_centroid(db, person_id)

    return {"status": "created", "person_id": person_id, "uuid": person_uuid}


@router.post("/{person_id}/add-cluster/{cluster_id}")
async def add_cluster_to_person(person_id: int, cluster_id: int):
    """Assign an existing cluster to an existing person (merge path)."""
    async with get_db() as db:
        existing = await (
            await db.execute("SELECT id FROM persons WHERE id=? AND is_merged=0", (person_id,))
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Person not found")

        await db.execute(
            "UPDATE clusters SET person_id=? WHERE id=?", (person_id, cluster_id)
        )
        await db.execute(
            "UPDATE faces SET person_id=? WHERE cluster_id=?", (person_id, cluster_id)
        )
        await _sync_photo_count(db, person_id)

        # Clear any rejection record — user just accepted this cluster
        await db.execute(
            "DELETE FROM rejected_suggestions WHERE person_id=? AND cluster_id=?",
            (person_id, cluster_id),
        )

        # Queue photos for writeback
        await db.execute("""
            INSERT OR REPLACE INTO writeback_queue (media_file_id)
            SELECT DISTINCT media_file_id FROM faces WHERE cluster_id=?
        """, (cluster_id,))

        # Refresh centroid with the newly-added cluster's embeddings
        await update_person_centroid(db, person_id)

    return {"status": "merged", "person_id": person_id}


# ---------------------------------------------------------------------------
# Proactive merge suggestions
# ---------------------------------------------------------------------------

@router.get("/{person_id}/merge-suggestions")
async def get_merge_suggestions(person_id: int, limit: int = 1):
    """
    Return up to `limit` unnamed clusters that may contain the same person,
    ranked by cosine similarity between embedding centroids.
    Only clusters not previously rejected for this person are returned.
    """
    async with get_db() as db:
        person = await (
            await db.execute(
                "SELECT id, name FROM persons WHERE id=? AND is_merged=0", (person_id,)
            )
        ).fetchone()
        if not person:
            raise HTTPException(status_code=404, detail="Person not found")

        # Person's embeddings
        emb_rows = await db.execute_fetchall("""
            SELECT e.vector FROM embeddings e
            JOIN faces f ON f.id = e.face_id
            WHERE f.person_id = ?
        """, (person_id,))
        if not emb_rows:
            return []

        person_vecs = np.stack([
            np.frombuffer(r["vector"], dtype=np.float32) for r in emb_rows
        ])
        person_centroid = person_vecs.mean(axis=0)
        norm = np.linalg.norm(person_centroid)
        if norm > 0:
            person_centroid /= norm

        # Unnamed clusters not yet rejected for this person
        candidates = await db.execute_fetchall("""
            SELECT c.id AS cluster_id, c.member_count,
                   c.intra_similarity, c.is_high_conf,
                   MIN(f.thumbnail_path) AS representative_thumbnail
            FROM clusters c
            JOIN faces f ON f.cluster_id = c.id
            WHERE c.person_id IS NULL
              AND c.id NOT IN (
                  SELECT cluster_id FROM rejected_suggestions WHERE person_id = ?
              )
            GROUP BY c.id
        """, (person_id,))

        if not candidates:
            return []

        # Score each candidate
        scored: list[dict] = []
        for cand in candidates:
            cid = cand["cluster_id"]
            cand_embs = await db.execute_fetchall("""
                SELECT e.vector FROM embeddings e
                JOIN faces f ON f.id = e.face_id
                WHERE f.cluster_id = ?
            """, (cid,))
            if not cand_embs:
                continue
            cand_vecs = np.stack([
                np.frombuffer(r["vector"], dtype=np.float32) for r in cand_embs
            ])
            centroid = cand_vecs.mean(axis=0)
            n = np.linalg.norm(centroid)
            if n > 0:
                centroid /= n
            sim = float(np.dot(person_centroid, centroid))
            if sim >= _SUGGEST_THRESHOLD:
                scored.append({**dict(cand), "similarity": round(sim, 3)})

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:limit]


@router.post("/find-similar-all")
async def find_similar_all(request: FindSimilarAllRequest):
    """
    Scan every named person against every unnamed cluster.

    Clusters with cosine similarity >= auto_threshold are merged automatically.
    Clusters between SUGGEST_THRESHOLD and auto_threshold are returned as
    suggestions for the user to accept or reject interactively.

    Clusters previously rejected for a person are skipped.
    A cluster is only auto-merged once (first matching person wins).
    """
    auto_threshold = max(_SUGGEST_THRESHOLD, min(1.0, request.auto_threshold))

    auto_merged: list[dict] = []
    suggestions: list[dict] = []

    async with get_db() as db:
        named_persons = await db.execute_fetchall("""
            SELECT p.id, p.name,
                   (SELECT MIN(f2.thumbnail_path) FROM faces f2 WHERE f2.person_id = p.id) AS thumbnail
            FROM persons p
            WHERE p.name IS NOT NULL AND p.is_merged = 0 AND p.is_ignored = 0
        """)
        if not named_persons:
            return {"auto_merged": [], "suggestions": []}

        unnamed_clusters = await db.execute_fetchall("""
            SELECT c.id AS cluster_id, c.member_count, c.intra_similarity, c.is_high_conf,
                   MIN(f.thumbnail_path) AS representative_thumbnail
            FROM clusters c
            JOIN faces f ON f.cluster_id = c.id
            WHERE c.person_id IS NULL
            GROUP BY c.id
        """)
        if not unnamed_clusters:
            return {"auto_merged": [], "suggestions": []}

        # Pre-load unnamed cluster embeddings once (avoids N×M round-trips)
        cluster_centroids: dict[int, np.ndarray] = {}
        for cluster in unnamed_clusters:
            cid = cluster["cluster_id"]
            cand_embs = await db.execute_fetchall("""
                SELECT e.vector FROM embeddings e
                JOIN faces f ON f.id = e.face_id
                WHERE f.cluster_id = ?
            """, (cid,))
            if not cand_embs:
                continue
            vecs = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in cand_embs])
            c_vec = vecs.mean(axis=0)
            n = np.linalg.norm(c_vec)
            if n > 0:
                c_vec /= n
            cluster_centroids[cid] = c_vec

        cluster_by_id = {c["cluster_id"]: c for c in unnamed_clusters}

        # Track clusters already auto-merged this run — don't suggest them again
        already_merged_cluster_ids: set[int] = set()

        for person in named_persons:
            person_id = person["id"]
            person_name = person["name"]
            person_thumbnail = person["thumbnail"]

            emb_rows = await db.execute_fetchall("""
                SELECT e.vector FROM embeddings e
                JOIN faces f ON f.id = e.face_id
                WHERE f.person_id = ?
            """, (person_id,))
            if not emb_rows:
                continue

            p_vecs = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in emb_rows])
            p_centroid = p_vecs.mean(axis=0)
            norm = np.linalg.norm(p_centroid)
            if norm > 0:
                p_centroid /= norm

            rejected = await db.execute_fetchall(
                "SELECT cluster_id FROM rejected_suggestions WHERE person_id = ?", (person_id,)
            )
            rejected_ids = {r["cluster_id"] for r in rejected}

            for cid, c_vec in cluster_centroids.items():
                if cid in rejected_ids or cid in already_merged_cluster_ids:
                    continue
                sim = float(np.dot(p_centroid, c_vec))
                cluster = cluster_by_id[cid]

                if sim >= auto_threshold:
                    # Auto-merge: assign cluster to person
                    await db.execute("UPDATE clusters SET person_id=? WHERE id=?", (person_id, cid))
                    await db.execute("UPDATE faces SET person_id=? WHERE cluster_id=?", (person_id, cid))
                    await db.execute("""
                        DELETE FROM rejected_suggestions WHERE person_id=? AND cluster_id=?
                    """, (person_id, cid))
                    await db.execute("""
                        INSERT OR REPLACE INTO writeback_queue (media_file_id)
                        SELECT DISTINCT media_file_id FROM faces WHERE cluster_id=?
                    """, (cid,))
                    await _sync_photo_count(db, person_id)
                    await update_person_centroid(db, person_id)
                    already_merged_cluster_ids.add(cid)
                    auto_merged.append({
                        "person_id": person_id,
                        "person_name": person_name,
                        "cluster_id": cid,
                        "similarity": round(sim, 3),
                        "member_count": cluster["member_count"],
                    })

                elif sim >= _SUGGEST_THRESHOLD:
                    suggestions.append({
                        "person_id": person_id,
                        "person_name": person_name,
                        "person_thumbnail": person_thumbnail,
                        "cluster_id": cid,
                        "member_count": cluster["member_count"],
                        "intra_similarity": cluster["intra_similarity"],
                        "is_high_conf": cluster["is_high_conf"],
                        "representative_thumbnail": cluster["representative_thumbnail"],
                        "similarity": round(sim, 3),
                    })

    suggestions.sort(key=lambda x: x["similarity"], reverse=True)
    return {"auto_merged": auto_merged, "suggestions": suggestions}


@router.post("/{person_id}/reject-suggestion/{cluster_id}")
async def reject_merge_suggestion(person_id: int, cluster_id: int):
    """
    Record that the user said 'Different person' for this cluster.
    It will not be suggested again for this person.
    """
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO rejected_suggestions (person_id, cluster_id) VALUES (?,?)",
            (person_id, cluster_id),
        )
    return {"status": "rejected"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _sync_photo_count(db, person_id: int) -> None:
    """Keep the denormalised persons.photo_count column accurate."""
    await db.execute("""
        UPDATE persons
        SET photo_count = (
            SELECT COUNT(DISTINCT media_file_id) FROM faces WHERE person_id = ?
        )
        WHERE id = ?
    """, (person_id, person_id))


@router.get("/{person_id}/faces")
async def get_person_faces(person_id: int, limit: int = 60):
    """All face thumbnails assigned to a person, for review / false-positive removal."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT f.id, f.thumbnail_path, f.detection_conf,
                   f.media_file_id, mf.file_path, mf.date_taken
            FROM faces f
            JOIN media_files mf ON mf.id = f.media_file_id
            WHERE f.person_id = ?
            ORDER BY f.detection_conf DESC
            LIMIT ?
        """, (person_id, limit))
    return [dict(r) for r in rows]
