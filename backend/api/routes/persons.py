"""VIP API — Persons routes."""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from typing import Literal, Optional

import numpy as np
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from backend.database.db import get_db
from backend.database.identity import append_identity_event, get_current_cluster_id_for_face, get_current_person_id_for_cluster, link_cluster_to_person, relink_face_to_cluster
from backend.database.models import Person
from backend.pipeline.centroid import update_person_centroid, load_centroid
from backend.api.websocket import broadcast
from backend.profiles import get_current_profile_id, run_in_profile

# Minimum cosine similarity to surface a merge suggestion
_SUGGEST_THRESHOLD = 0.50

logger = logging.getLogger(__name__)
router = APIRouter()

UPDATE_CLUSTER_PERSON_SQL = "UPDATE clusters SET person_id=? WHERE id=?"
UPDATE_CLUSTER_FACES_PERSON_SQL = "UPDATE faces SET person_id=? WHERE cluster_id=?"


# ---------------------------------------------------------------------------
# Post-merge FAISS re-score
# ---------------------------------------------------------------------------

async def _rescore_after_person_update(person_id: int) -> None:
    """Query FAISS with the updated person centroid and surface new suggestions.

    Called after any user action that changes a person's face membership
    (name, merge, add-cluster).  The updated centroid is already written to
    the DB before this is called.

    Steps:
    1. Load the person's stored centroid from DB.
    2. Query the in-memory FAISS index for the k nearest face embeddings.
    3. For each hit that belongs to an unnamed cluster:
       - sim >= auto_name_threshold  → auto-assign silently (same as Phase 3b)
       - sim >= merge_suggest_threshold → broadcast as a suggestion card
    4. Broadcast the suggestions via WebSocket so the frontend can show cards.

    This runs as a fire-and-forget background task so the API response is
    instant — the WebSocket event arrives shortly after.
    """
    try:
        from backend.pipeline.ingest import _faiss
        from backend.database.settings_store import get as get_setting

        auto_th    = float(get_setting("auto_name_threshold"))
        suggest_th = float(get_setting("merge_suggest_threshold"))

        if _faiss.total == 0:
            return

        # Load the person's current centroid
        async with get_db() as db:
            p_row = await (await db.execute(
                "SELECT name, centroid FROM persons WHERE id=? AND is_merged=0",
                (person_id,),
            )).fetchone()
        if not p_row or not p_row["centroid"]:
            return

        person_name = p_row["name"]
        person_centroid = load_centroid(p_row["centroid"])

        # FAISS NN search — get the 30 closest face embeddings
        hits = _faiss.search(person_centroid, k=30, threshold=suggest_th)
        if not hits:
            return

        # Map hit face_ids → cluster info in one DB query
        hit_face_ids = [fid for fid, _ in hits]
        ph = ",".join("?" * len(hit_face_ids))
        async with get_db() as db:
            rows = await db.execute_fetchall(f"""
                SELECT f.id AS face_id,
                       COALESCE(c_current.id, f.cluster_id) AS cluster_id,
                       current_person.id AS person_id,
                       f.thumbnail_path,
                       c.member_count,
                       owner_person.id AS cluster_person_id,
                       MIN(f2.thumbnail_path) AS cluster_thumb
                FROM faces f
                LEFT JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                LEFT JOIN clusters c_current ON c_current.cluster_guid = fcc.cluster_guid
                LEFT JOIN clusters c ON c.id = COALESCE(c_current.id, f.cluster_id)
                LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = COALESCE(fcc.cluster_guid, c.cluster_guid)
                LEFT JOIN persons owner_person ON owner_person.person_guid = cpc.person_guid
                    AND owner_person.is_merged = 0
                LEFT JOIN persons current_person ON current_person.id = f.person_id
                    AND current_person.is_merged = 0
                LEFT JOIN faces f2 ON f2.cluster_id = COALESCE(c_current.id, f.cluster_id)
                    AND f2.thumbnail_path IS NOT NULL
                WHERE f.id IN ({ph})
                  AND COALESCE(c_current.id, f.cluster_id) IS NOT NULL
                GROUP BY f.id
            """, hit_face_ids)

            rejected_rows = await db.execute_fetchall(
                "SELECT cluster_id FROM rejected_suggestions WHERE person_id=?",
                (person_id,),
            )

        rejected_cluster_ids = {r["cluster_id"] for r in rejected_rows}

        hit_map: dict[int, dict] = {r["face_id"]: dict(r) for r in rows}

        suggestions: list[dict] = []
        auto_named = 0
        seen_clusters: set[int] = set()

        for face_id, sim in hits:
            info = hit_map.get(face_id)
            if info is None:
                continue
            cluster_id = info["cluster_id"]
            if cluster_id in seen_clusters:
                continue
            # Skip faces that already belong to this person or another named person
            if info["cluster_person_id"] is not None:
                # Allow if it belongs to THIS person (already named, skip)
                # Skip if it belongs to a DIFFERENT person
                seen_clusters.add(cluster_id)
                continue
            if cluster_id in rejected_cluster_ids:
                seen_clusters.add(cluster_id)
                continue

            seen_clusters.add(cluster_id)

            if sim >= auto_th:
                # Auto-assign this unnamed cluster to the person
                async with get_db() as db:
                    await db.execute(
                        UPDATE_CLUSTER_PERSON_SQL,
                        (person_id, cluster_id),
                    )
                    await db.execute(
                        UPDATE_CLUSTER_FACES_PERSON_SQL,
                        (person_id, cluster_id),
                    )
                    await db.execute("""
                        UPDATE persons SET photo_count=(
                            SELECT COUNT(DISTINCT media_file_id) FROM faces WHERE person_id=?
                        ) WHERE id=?
                    """, (person_id, person_id))
                    await db.execute("""
                        INSERT OR REPLACE INTO writeback_queue (media_file_id)
                        SELECT DISTINCT media_file_id FROM faces WHERE cluster_id=?
                    """, (cluster_id,))
                    await update_person_centroid(db, person_id)
                auto_named += 1
                logger.info(
                    "Post-merge FAISS: auto-named cluster %d → '%s' (sim=%.3f)",
                    cluster_id, person_name, sim,
                )

            elif sim >= suggest_th:
                suggestions.append({
                    "person_id":       person_id,
                    "person_name":     person_name,
                    "person_face_id":  None,
                    "cluster_id":      cluster_id,
                    "cluster_face_id": face_id,
                    "similarity":      round(sim, 3),
                    "member_count":    info["member_count"] or 1,
                })

        if suggestions:
            # Deduplicate and cap at 5 — don't flood the UI
            suggestions.sort(key=lambda s: s["similarity"], reverse=True)
            await broadcast("merge_suggestions", suggestions=suggestions[:5])
            logger.info(
                "Post-merge FAISS: %d auto-named, %d suggestions for '%s'",
                auto_named, len(suggestions), person_name,
            )

    except Exception as exc:
        # Non-fatal — log and continue; the user action already succeeded
        logger.warning("_rescore_after_person_update failed (non-fatal): %s", exc)


async def _update_cooccurrence_for_person(person_id: int) -> None:
    """Incrementally refresh co-occurrence edges for a newly named/updated person.

    Called as a background task after any naming or merge action.
    Only touches edges where this person is one of the two participants,
    so it is much faster than a full rebuild.

    Steps:
    1. Delete all existing edges that involve person_id.
    2. Recompute and insert fresh edges by joining this person's faces
       against all other named persons sharing the same photos.
    """
    try:
        async with get_db() as db:
            # Verify the person is still active (not merged/ignored)
            p = await (await db.execute(
                "SELECT id FROM persons WHERE id=? AND is_merged=0 AND is_ignored=0",
                (person_id,),
            )).fetchone()
            if not p:
                return

            # Remove stale edges for this person
            await db.execute(
                "DELETE FROM person_cooccurrence WHERE person_a_id=? OR person_b_id=?",
                (person_id, person_id),
            )

            # Recompute edges: find all named persons that share a photo with
            # this person, using the same canonical (a < b) ordering.
            await db.execute("""
                INSERT INTO person_cooccurrence (person_a_id, person_b_id, count, last_seen_at)
                SELECT
                    pairs.pa,
                    pairs.pb,
                    COUNT(DISTINCT pairs.media_file_id) AS count,
                    COALESCE(MAX(pairs.date_taken), datetime('now')) AS last_seen_at
                FROM (
                    SELECT
                        CASE WHEN f1.person_id < f2.person_id
                             THEN f1.person_id ELSE f2.person_id END AS pa,
                        CASE WHEN f1.person_id < f2.person_id
                             THEN f2.person_id ELSE f1.person_id END AS pb,
                        f1.media_file_id,
                        m.date_taken
                    FROM faces f1
                    JOIN faces f2
                      ON  f2.media_file_id = f1.media_file_id
                      AND f2.person_id     != f1.person_id
                      AND f2.person_id     IS NOT NULL
                    JOIN media_files m ON m.id = f1.media_file_id
                    WHERE f1.person_id = ?
                ) AS pairs
                JOIN persons pa ON pa.id = pairs.pa
                               AND pa.is_merged = 0 AND pa.is_ignored = 0
                JOIN persons pb ON pb.id = pairs.pb
                               AND pb.is_merged = 0 AND pb.is_ignored = 0
                GROUP BY pairs.pa, pairs.pb
                HAVING COUNT(DISTINCT pairs.media_file_id) >= 1
            """, (person_id,))

            row = await (await db.execute(
                "SELECT COUNT(*) AS n FROM person_cooccurrence WHERE person_a_id=? OR person_b_id=?",
                (person_id, person_id),
            )).fetchone()
            edge_count = row["n"] if row else 0

        logger.debug(
            "Co-occurrence updated for person %d: %d edges", person_id, edge_count
        )
    except Exception as exc:
        logger.warning("_update_cooccurrence_for_person failed (non-fatal): %s", exc)


class NamePersonRequest(BaseModel):
    name: str


class MergeRequest(BaseModel):
    into_person_id: int     # merge source → target (kept for backward compat)


class MergeNamedPersonsRequest(BaseModel):
    new_name: Optional[str] = None   # None = keep the survivor's current name


class FindSimilarAllRequest(BaseModel):
    auto_threshold: float = 0.85   # Clusters >= this similarity are merged automatically


class IgnoreSuggestionRequest(BaseModel):
    action: Literal["delete", "ignore"]
    threshold: float = 0.85
    limit: int = 8


class IgnoredPersonSuggestionRequest(BaseModel):
    threshold: float = 0.85
    limit: int = 8


@router.get("")
async def list_persons():
    """All persons — named and unnamed (clusters awaiting a name)."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT p.id, p.uuid, p.person_guid, p.name, p.created_at, p.named_at,
                   p.is_merged, p.merged_into_id,
                   COUNT(DISTINCT f.media_file_id)            AS photo_count,
                   (SELECT COUNT(*) FROM persons p2
                    WHERE p2.merged_into_id = p.id
                      AND p2.is_merged = 1)                   AS merge_sources_count,
                   COALESCE(pf.thumbnail_path, MIN(f.thumbnail_path))
                                                              AS representative_thumbnail,
                   CASE WHEN p.name IS NOT NULL AND EXISTS (
                       SELECT 1 FROM writeback_queue wq
                       JOIN faces f2 ON f2.media_file_id = wq.media_file_id
                       JOIN v_face_cluster_current fcc2 ON fcc2.face_guid = f2.face_guid
                       JOIN v_cluster_person_current cpc2 ON cpc2.cluster_guid = fcc2.cluster_guid
                       WHERE cpc2.person_guid = p.person_guid AND wq.status = 'written'
                   ) THEN 1 ELSE 0 END                        AS name_written
            FROM persons p
            LEFT JOIN v_cluster_person_current cpc ON cpc.person_guid = p.person_guid
            LEFT JOIN clusters c ON c.cluster_guid = cpc.cluster_guid
            LEFT JOIN faces f  ON f.cluster_id = c.id
            LEFT JOIN faces pf ON pf.id = p.portrait_face_id
            WHERE p.is_merged = 0 AND p.is_ignored = 0
            GROUP BY p.id
            ORDER BY photo_count DESC
        """)
    return [dict(r) for r in rows]


@router.get("/unnamed")
async def list_unnamed_clusters():
    """Clusters with no current non-merged owner person."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT c.id, c.cluster_guid, c.member_count, c.intra_similarity, c.is_high_conf,
                   MIN(f.thumbnail_path) as representative_thumbnail
            FROM clusters c
            LEFT JOIN faces f ON f.cluster_id = c.id AND f.thumbnail_path IS NOT NULL
            LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = c.cluster_guid
            LEFT JOIN persons owner_person ON owner_person.person_guid = cpc.person_guid AND owner_person.is_merged = 0
            WHERE owner_person.id IS NULL
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
        source = await _load_cluster_for_ignore_actions(db, cluster_id)
        if source is None:
            return []
        return await _score_similar_unnamed_clusters(
            db,
            source["vector"],
            exclude_cluster_ids={cluster_id},
            limit=limit,
        )


def _normalise_vector(vec: np.ndarray) -> np.ndarray:
    out = vec.copy()
    norm = np.linalg.norm(out)
    if norm > 0:
        out /= norm
    return out


async def _load_cluster_for_ignore_actions(db, cluster_id: int) -> dict | None:
    row = await (
        await db.execute(
            """
                 SELECT c.id, c.member_count, c.centroid,
                     p.id AS person_id,
                   MIN(f.thumbnail_path) AS representative_thumbnail
            FROM clusters c
            LEFT JOIN faces f ON f.cluster_id = c.id AND f.thumbnail_path IS NOT NULL
                 LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = c.cluster_guid
                 LEFT JOIN persons p ON p.person_guid = cpc.person_guid AND p.is_merged = 0
            WHERE c.id = ?
            GROUP BY c.id
            """,
            (cluster_id,),
        )
    ).fetchone()
    if not row or row["person_id"] is not None or not row["centroid"]:
        return None
    return {
        "cluster_id": int(row["id"]),
        "member_count": int(row["member_count"] or 0),
        "representative_thumbnail": row["representative_thumbnail"],
        "vector": _normalise_vector(np.frombuffer(row["centroid"], dtype=np.float32)),
    }


async def _score_similar_unnamed_clusters(
    db,
    source_vec: np.ndarray,
    *,
    exclude_cluster_ids: set[int] | None = None,
    rejected_for_person_id: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    exclude_cluster_ids = exclude_cluster_ids or set()
    candidates = await db.execute_fetchall(
        """
        SELECT c.id AS cluster_id, c.member_count, c.intra_similarity,
               c.is_high_conf, c.centroid,
               MIN(f.thumbnail_path) AS representative_thumbnail
        FROM clusters c
        LEFT JOIN faces f ON f.cluster_id = c.id AND f.thumbnail_path IS NOT NULL
        LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = c.cluster_guid
        LEFT JOIN persons owner_person ON owner_person.person_guid = cpc.person_guid AND owner_person.is_merged = 0
        WHERE owner_person.id IS NULL
        GROUP BY c.id
        """
    )

    rejected_cluster_ids: set[int] = set()
    if rejected_for_person_id is not None:
        rejected_rows = await db.execute_fetchall(
            "SELECT cluster_id FROM rejected_suggestions WHERE person_id=?",
            (rejected_for_person_id,),
        )
        rejected_cluster_ids = {int(r["cluster_id"]) for r in rejected_rows}

    scored: list[dict] = []
    for cand in candidates:
        cid = int(cand["cluster_id"])
        if cid in exclude_cluster_ids or cid in rejected_cluster_ids or not cand["centroid"]:
            continue
        vec = _normalise_vector(np.frombuffer(cand["centroid"], dtype=np.float32))
        sim = float(np.dot(source_vec, vec))
        scored.append({
            "cluster_id": cid,
            "member_count": int(cand["member_count"] or 0),
            "intra_similarity": cand["intra_similarity"],
            "is_high_conf": int(cand["is_high_conf"] or 0),
            "representative_thumbnail": cand["representative_thumbnail"],
            "similarity": round(sim, 3),
        })

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:limit] if limit is not None else scored


async def _create_ignored_person(db, source_vec: np.ndarray | None = None, centroid_n: int = 0) -> int:
    new_uuid = str(_uuid.uuid4())
    cursor = await db.execute(
        "INSERT INTO persons (uuid, is_ignored, centroid, centroid_n) VALUES (?, 1, ?, ?)",
        (new_uuid, (source_vec.tobytes() if source_vec is not None else None), max(0, int(centroid_n))),
    )
    return int(cursor.lastrowid)


async def _assign_cluster_to_ignored_person(db, person_id: int, cluster_id: int) -> None:
    await db.execute(UPDATE_CLUSTER_PERSON_SQL, (person_id, cluster_id))
    await db.execute(UPDATE_CLUSTER_FACES_PERSON_SQL, (person_id, cluster_id))
    await link_cluster_to_person(
        db,
        cluster_id=cluster_id,
        person_id=person_id,
        source="manual_ignore_cluster",
        actor="api.ignore_cluster",
    )
    await _sync_photo_count(db, person_id)
    await db.execute(
        "DELETE FROM rejected_suggestions WHERE person_id=? AND cluster_id=?",
        (person_id, cluster_id),
    )
    await update_person_centroid(db, person_id)


async def _delete_cluster_and_release_faces(db, cluster_id: int) -> None:
    face_rows = await db.execute_fetchall(
        "SELECT id FROM faces WHERE cluster_id=?",
        (cluster_id,),
    )
    for row in face_rows:
        await relink_face_to_cluster(
            db,
            face_id=int(row["id"]),
            cluster_id=None,
            reason="cluster_deleted",
            actor="api.delete_cluster",
        )
    await link_cluster_to_person(
        db,
        cluster_id=cluster_id,
        person_id=None,
        source="cluster_deleted",
        actor="api.delete_cluster",
    )
    await db.execute("UPDATE faces SET cluster_id=NULL WHERE cluster_id=?", (cluster_id,))
    await db.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))


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
        owner_person_id = await get_current_person_id_for_cluster(db, cluster_id)
        if owner_person_id is not None:
            # Allow deleting attached-to-ignored-person clusters; block named-person clusters.
            p = await (
                await db.execute(
                    "SELECT is_ignored FROM persons WHERE id=?", (owner_person_id,)
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
                    """
                    SELECT COUNT(*) AS n
                    FROM v_cluster_person_current cpc
                    JOIN persons p ON p.person_guid = cpc.person_guid
                    JOIN clusters c ON c.cluster_guid = cpc.cluster_guid
                    WHERE p.id=? AND c.id != ?
                    """,
                    (owner_person_id, cluster_id),
                )
            ).fetchone()
            if remaining and remaining["n"] == 0:
                await db.execute(
                    "DELETE FROM persons WHERE id=? AND is_ignored=1",
                    (owner_person_id,),
                )
        await _delete_cluster_and_release_faces(db, cluster_id)
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
                "SELECT id FROM clusters WHERE id=?", (cluster_id,)
            )
        ).fetchone()
        if not row or await get_current_person_id_for_cluster(db, cluster_id) is not None:
            raise HTTPException(
                status_code=404, detail="Cluster not found or already assigned to a person."
            )

        person_id = await _create_ignored_person(db)
        await _assign_cluster_to_ignored_person(db, person_id, cluster_id)

    return {"status": "ignored", "cluster_id": cluster_id, "person_id": person_id}


@router.post("/clusters/{cluster_id}/ignore-suggestions")
async def ignore_cluster_with_suggestions(cluster_id: int, request: IgnoreSuggestionRequest):
    threshold = max(_SUGGEST_THRESHOLD, min(0.99, float(request.threshold or 0.85)))
    limit = max(1, min(int(request.limit or 8), 24))

    async with get_db() as db:
        source = await _load_cluster_for_ignore_actions(db, cluster_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Cluster not found or already assigned to a person.")

        ignored_person_id = await _create_ignored_person(
            db,
            source_vec=source["vector"] if request.action == "delete" else None,
            centroid_n=source["member_count"],
        )

        if request.action == "ignore":
            await _assign_cluster_to_ignored_person(db, ignored_person_id, cluster_id)
        else:
            await _delete_cluster_and_release_faces(db, cluster_id)

        scored = await _score_similar_unnamed_clusters(
            db,
            source["vector"],
            exclude_cluster_ids={cluster_id},
            rejected_for_person_id=ignored_person_id,
        )

        auto_ignored: list[dict] = []
        suggestions: list[dict] = []
        for item in scored:
            if item["similarity"] >= threshold:
                await _assign_cluster_to_ignored_person(db, ignored_person_id, int(item["cluster_id"]))
                auto_ignored.append(item)
            elif item["similarity"] >= _SUGGEST_THRESHOLD and len(suggestions) < limit:
                suggestions.append(item)

    return {
        "status": "ok",
        "action": request.action,
        "person_id": ignored_person_id,
        "threshold": threshold,
        "auto_ignored": auto_ignored,
        "suggestions": suggestions,
    }


@router.get("/ignored")
async def list_ignored_persons():
    """
    Return all always-ignored persons.

    Each entry has a representative face thumbnail so the user can see
    which face they previously chose to hide.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT p.id, p.uuid, p.created_at,
                   COUNT(DISTINCT f.media_file_id) AS photo_count,
                   COUNT(DISTINCT c.id)            AS cluster_count,
                   MIN(f.thumbnail_path)           AS representative_thumbnail
            FROM persons p
            LEFT JOIN v_cluster_person_current cpc ON cpc.person_guid = p.person_guid
            LEFT JOIN clusters c ON c.cluster_guid = cpc.cluster_guid
            LEFT JOIN faces f ON f.cluster_id = c.id
            WHERE p.is_ignored = 1 AND p.is_merged = 0
            GROUP BY p.id
            ORDER BY photo_count DESC
        """)
    return [dict(r) for r in rows]


@router.post("/{person_id}/unignore")
async def unignore_person(person_id: int):
    """
    Restore an always-ignored person back to the unnamed cluster pool.

    Detaches all clusters and faces from the ignored person record, then
    deletes it.  The freed clusters immediately appear in the Unnamed Faces
    tab and will be re-evaluated normally on the next pipeline run.
    """
    async with get_db() as db:
        row = await (
            await db.execute(
                "SELECT id FROM persons WHERE id=? AND is_ignored=1 AND is_merged=0",
                (person_id,),
            )
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Ignored person not found.")

        # Detach faces → they go back to unowned pool
        await db.execute(
            "UPDATE faces SET person_id=NULL WHERE person_id=?", (person_id,)
        )
        # Detach clusters → they return to the unnamed cluster list
        cluster_rows = await db.execute_fetchall(
            """
            SELECT c.id
            FROM v_cluster_person_current cpc
            JOIN persons p ON p.person_guid = cpc.person_guid
            JOIN clusters c ON c.cluster_guid = cpc.cluster_guid
            WHERE p.id=?
            """,
            (person_id,),
        )
        for cluster_row in cluster_rows:
            await link_cluster_to_person(
                db,
                cluster_id=int(cluster_row["id"]),
                person_id=None,
                source="manual_unignore",
                actor="api.unignore_person",
            )
        await db.execute(
            "UPDATE clusters SET person_id=NULL WHERE person_id=?", (person_id,)
        )
        # Remove the hidden person record
        await db.execute(
            "DELETE FROM persons WHERE id=? AND is_ignored=1", (person_id,)
        )

    return {"status": "restored", "person_id": person_id}


@router.post("/ignored/{person_id}/suggestions")
async def ignored_person_suggestions(person_id: int, request: IgnoredPersonSuggestionRequest):
    """
    For an already-ignored person, find visually similar unnamed clusters.

    Used by the Photo Detail "Ignore all unnamed faces" flow where the primary
    ignore action happens first and similar-face suggestions are offered after.
    """
    threshold = max(_SUGGEST_THRESHOLD, min(0.99, float(request.threshold or 0.85)))
    limit = max(1, min(int(request.limit or 8), 24))

    async with get_db() as db:
        row = await (
            await db.execute(
                """
                SELECT id, is_ignored, centroid
                FROM persons
                WHERE id=? AND is_merged=0
                """,
                (person_id,),
            )
        ).fetchone()
        if not row or not row["is_ignored"]:
            raise HTTPException(status_code=404, detail="Ignored person not found")
        if not row["centroid"]:
            return {
                "status": "ok",
                "action": "ignore",
                "person_id": person_id,
                "threshold": threshold,
                "auto_ignored": [],
                "suggestions": [],
            }

        source_vec = _normalise_vector(np.frombuffer(row["centroid"], dtype=np.float32))
        scored = await _score_similar_unnamed_clusters(
            db,
            source_vec,
            rejected_for_person_id=person_id,
        )

        auto_ignored: list[dict] = []
        suggestions: list[dict] = []
        for item in scored:
            if item["similarity"] >= threshold:
                await _assign_cluster_to_ignored_person(db, person_id, int(item["cluster_id"]))
                auto_ignored.append(item)
            elif item["similarity"] >= _SUGGEST_THRESHOLD and len(suggestions) < limit:
                suggestions.append(item)

    return {
        "status": "ok",
        "action": "ignore",
        "person_id": person_id,
        "threshold": threshold,
        "auto_ignored": auto_ignored,
        "suggestions": suggestions,
    }


@router.delete("/{person_id}")
async def delete_person(person_id: int, background_tasks: BackgroundTasks):
    """
    Un-name a person: remove their name assignment and release all associated
    clusters back to the unnamed pool.

    Does NOT delete faces or embeddings — the face detections are kept and
    will reappear in the Unnamed Faces tab after the next cluster run.
    """
    async with get_db() as db:
        person = await (
            await db.execute(
                "SELECT id, name, is_merged FROM persons WHERE id=?", (person_id,)
            )
        ).fetchone()
        if not person:
            raise HTTPException(status_code=404, detail="Person not found")
        if person["is_merged"]:
            raise HTTPException(status_code=400, detail="Cannot un-name a merged person record.")

        # 1. Queue affected media for writeback BEFORE unlinking (so we can find them)
        await db.execute("""
            INSERT OR REPLACE INTO writeback_queue (media_file_id)
            SELECT DISTINCT f.media_file_id
            FROM faces f
            JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
            JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
            JOIN persons p ON p.person_guid = cpc.person_guid
            WHERE p.id=?
        """, (person_id,))

        # 2. Unlink faces from person
        await db.execute("UPDATE faces SET person_id=NULL WHERE person_id=?", (person_id,))

        # 3. Unlink clusters from person
        cluster_rows = await db.execute_fetchall(
            """
            SELECT c.id
            FROM v_cluster_person_current cpc
            JOIN persons p ON p.person_guid = cpc.person_guid
            JOIN clusters c ON c.cluster_guid = cpc.cluster_guid
            WHERE p.id=?
            """,
            (person_id,),
        )
        for cluster_row in cluster_rows:
            await link_cluster_to_person(
                db,
                cluster_id=int(cluster_row["id"]),
                person_id=None,
                source="manual_delete_person",
                actor="api.delete_person",
            )
        await db.execute("UPDATE clusters SET person_id=NULL WHERE person_id=?", (person_id,))

        # 4. Remove co-occurrence edges
        await db.execute(
            "DELETE FROM person_cooccurrence WHERE person_a_id=? OR person_b_id=?",
            (person_id, person_id),
        )

        # 5. Remove suggestion rejections
        await db.execute(
            "DELETE FROM rejected_suggestions WHERE person_id=?", (person_id,)
        )

        # 6. Delete the person row itself
        await db.execute("DELETE FROM persons WHERE id=?", (person_id,))

    logger.info("Person %d ('%s') un-named — clusters released to unnamed pool", person_id, person["name"])
    return {"status": "deleted", "person_id": person_id}


@router.patch("/{person_id}/name")
async def name_person(person_id: int, req: NamePersonRequest, background_tasks: BackgroundTasks):
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
            JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
            JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
            JOIN persons p ON p.person_guid = cpc.person_guid
            WHERE p.id = ?
        """, (person_id,))

        # Persist centroid so this person is recognisable in future scans
        await update_person_centroid(db, person_id)

    profile_id = get_current_profile_id()
    background_tasks.add_task(run_in_profile, profile_id, _rescore_after_person_update, person_id)
    background_tasks.add_task(run_in_profile, profile_id, _update_cooccurrence_for_person, person_id)
    return {"status": "ok", "name": req.name}


@router.post("/merge")
async def merge_persons(req: MergeRequest, source_id: int):
    """Merge two persons (legacy endpoint — use /{a}/merge-with/{b} instead)."""
    async with get_db() as db:
        cluster_rows = await db.execute_fetchall(
            """
            SELECT c.id
            FROM v_cluster_person_current cpc
            JOIN persons p ON p.person_guid = cpc.person_guid
            JOIN clusters c ON c.cluster_guid = cpc.cluster_guid
            WHERE p.id=?
            """,
            (source_id,),
        )
        for cluster_row in cluster_rows:
            cluster_id = int(cluster_row["id"])
            await db.execute(UPDATE_CLUSTER_PERSON_SQL, (req.into_person_id, cluster_id))
            await db.execute(UPDATE_CLUSTER_FACES_PERSON_SQL, (req.into_person_id, cluster_id))
            await link_cluster_to_person(
                db,
                cluster_id=cluster_id,
                person_id=req.into_person_id,
                source="legacy_merge",
                actor="api.merge_persons",
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
    background_tasks: BackgroundTasks,
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
                """
                SELECT COUNT(DISTINCT f.media_file_id) AS n
                FROM faces f
                JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                JOIN persons p ON p.person_guid = cpc.person_guid
                WHERE p.id=?
                """,
                (person_a_id,),
            )
        ).fetchone()
        count_b_row = await (
            await db.execute(
                """
                SELECT COUNT(DISTINCT f.media_file_id) AS n
                FROM faces f
                JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                JOIN persons p ON p.person_guid = cpc.person_guid
                WHERE p.id=?
                """,
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

        loser_clusters = await db.execute_fetchall(
            """
            SELECT c.id
            FROM v_cluster_person_current cpc
            JOIN persons p ON p.person_guid = cpc.person_guid
            JOIN clusters c ON c.cluster_guid = cpc.cluster_guid
            WHERE p.id=?
            """,
            (loser_id,),
        )
        for loser_cluster in loser_clusters:
            cluster_id = int(loser_cluster["id"])
            await db.execute(UPDATE_CLUSTER_PERSON_SQL, (survivor_id, cluster_id))
            await db.execute(UPDATE_CLUSTER_FACES_PERSON_SQL, (survivor_id, cluster_id))
            await link_cluster_to_person(
                db,
                cluster_id=cluster_id,
                person_id=survivor_id,
                source="manual_merge_person",
                actor="api.merge_named_persons",
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
            SELECT DISTINCT f.media_file_id
            FROM faces f
            JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
            JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
            JOIN persons p ON p.person_guid = cpc.person_guid
            WHERE p.id=?
        """, (survivor_id,))

        # Count the queued photos for the response summary.
        queued_row = await (
            await db.execute(
                """
                SELECT COUNT(DISTINCT f.media_file_id) AS n
                FROM faces f
                JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                JOIN persons p ON p.person_guid = cpc.person_guid
                WHERE p.id=?
                """,
                (survivor_id,),
            )
        ).fetchone()
        photos_queued = queued_row["n"] if queued_row else 0

        # 7. Refresh the denormalised photo_count column.
        await _sync_photo_count(db, survivor_id)

        # 8. Recompute centroid from all merged embeddings.
        await update_person_centroid(db, survivor_id)

    profile_id = get_current_profile_id()
    background_tasks.add_task(run_in_profile, profile_id, _rescore_after_person_update, survivor_id)
    background_tasks.add_task(run_in_profile, profile_id, _update_cooccurrence_for_person, survivor_id)
    return {
        "status": "merged",
        "survivor_id": survivor_id,
        "survivor_name": effective_name,
        "absorbed_id": loser_id,
        "photos_queued_for_writeback": photos_queued,
    }


class NameClusterRequest(BaseModel):
    name: str


@router.post("/assign-face/{face_id}")
async def assign_lone_face_to_person(face_id: int, req: NameClusterRequest, background_tasks: BackgroundTasks):
    """
    Assign a lone face (cluster_id=NULL) directly to a named person.
    Used when a face was ejected from its previous person/cluster and the user
    now wants to label it correctly from the photo detail view.
    Creates the person if no person with that name exists yet.
    """
    async with get_db() as db:
        face_row = await (
            await db.execute(
                "SELECT id, media_file_id, cluster_id FROM faces WHERE id=?", (face_id,)
            )
        ).fetchone()
        if not face_row:
            raise HTTPException(status_code=404, detail="Face not found")

        name = req.name.strip()

        # Find or create person by name (case-insensitive)
        existing = await (
            await db.execute(
                "SELECT id FROM persons WHERE lower(name)=lower(?) AND is_merged=0 AND is_ignored=0",
                (name,)
            )
        ).fetchone()

        if existing:
            person_id = existing["id"]
        else:
            person_uuid = str(_uuid.uuid4())
            cursor = await db.execute(
                "INSERT INTO persons (uuid, name, named_at) VALUES (?, ?, datetime('now'))",
                (person_uuid, name),
            )
            person_id = cursor.lastrowid

        cluster_id = face_row["cluster_id"] or await get_current_cluster_id_for_face(db, face_id)
        if cluster_id is None:
            emb_row = await (
                await db.execute("SELECT vector FROM embeddings WHERE face_id=?", (face_id,))
            ).fetchone()
            if emb_row and emb_row["vector"]:
                cursor = await db.execute(
                    """
                    INSERT INTO clusters (centroid, member_count, intra_similarity, is_high_conf)
                    VALUES (?, 1, 1.0, 0)
                    """,
                    (emb_row["vector"],),
                )
                cluster_id = int(cursor.lastrowid)
                await db.execute("UPDATE faces SET cluster_id=? WHERE id=?", (cluster_id, face_id))
                await relink_face_to_cluster(
                    db,
                    face_id=face_id,
                    cluster_id=cluster_id,
                    reason="name_lone_face",
                    actor="api.assign_face",
                )

        await db.execute(
            "UPDATE faces SET person_id=? WHERE id=?", (person_id, face_id)
        )
        if cluster_id is not None:
            await db.execute(UPDATE_CLUSTER_PERSON_SQL, (person_id, cluster_id))
            await db.execute(UPDATE_CLUSTER_FACES_PERSON_SQL, (person_id, cluster_id))
            await link_cluster_to_person(
                db,
                cluster_id=cluster_id,
                person_id=person_id,
                source="manual_assign_face",
                actor="api.assign_face",
            )
        await append_identity_event(
            db,
            "face_person_assigned",
            actor="api.assign_face",
            payload={"face_id": face_id, "person_id": person_id, "name": name},
        )

        await _sync_photo_count(db, person_id)

        if face_row["media_file_id"]:
            await db.execute("""
                INSERT OR REPLACE INTO writeback_queue (media_file_id, status, queued_at)
                VALUES (?, 'pending', datetime('now'))
            """, (face_row["media_file_id"],))

        await update_person_centroid(db, person_id)

    profile_id = get_current_profile_id()
    background_tasks.add_task(run_in_profile, profile_id, _rescore_after_person_update, person_id)
    background_tasks.add_task(run_in_profile, profile_id, _update_cooccurrence_for_person, person_id)
    return {"status": "assigned", "face_id": face_id, "person_id": person_id}


@router.post("/from-cluster/{cluster_id}")
async def create_person_from_cluster(cluster_id: int, req: NameClusterRequest, background_tasks: BackgroundTasks):
    """Create a named person from a cluster in one step."""
    async with get_db() as db:
        person_uuid = str(_uuid.uuid4())
        cursor = await db.execute("""
            INSERT INTO persons (uuid, name, named_at) VALUES (?, ?, datetime('now'))
        """, (person_uuid, req.name.strip()))
        person_id = cursor.lastrowid

        await db.execute(
            UPDATE_CLUSTER_PERSON_SQL, (person_id, cluster_id)
        )
        await db.execute(
            UPDATE_CLUSTER_FACES_PERSON_SQL, (person_id, cluster_id)
        )
        await link_cluster_to_person(
            db,
            cluster_id=cluster_id,
            person_id=person_id,
            source="manual_create_from_cluster",
            actor="api.create_person_from_cluster",
        )
        await _sync_photo_count(db, person_id)

        # Queue photos for writeback
        await db.execute("""
            INSERT OR REPLACE INTO writeback_queue (media_file_id)
            SELECT DISTINCT media_file_id FROM faces WHERE cluster_id=?
        """, (cluster_id,))

        # Persist centroid — this person now has embeddings to match against
        await update_person_centroid(db, person_id)

    profile_id = get_current_profile_id()
    background_tasks.add_task(run_in_profile, profile_id, _rescore_after_person_update, person_id)
    background_tasks.add_task(run_in_profile, profile_id, _update_cooccurrence_for_person, person_id)
    return {"status": "created", "person_id": person_id, "uuid": person_uuid}


@router.post("/{person_id}/add-cluster/{cluster_id}")
async def add_cluster_to_person(person_id: int, cluster_id: int, background_tasks: BackgroundTasks):
    """Assign an existing cluster to an existing person (merge path)."""
    async with get_db() as db:
        existing = await (
            await db.execute("SELECT id FROM persons WHERE id=? AND is_merged=0", (person_id,))
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Person not found")

        await db.execute(
            UPDATE_CLUSTER_PERSON_SQL, (person_id, cluster_id)
        )
        await db.execute(
            UPDATE_CLUSTER_FACES_PERSON_SQL, (person_id, cluster_id)
        )
        await link_cluster_to_person(
            db,
            cluster_id=cluster_id,
            person_id=person_id,
            source="manual_merge_cluster",
            actor="api.add_cluster_to_person",
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

    profile_id = get_current_profile_id()
    background_tasks.add_task(run_in_profile, profile_id, _rescore_after_person_update, person_id)
    background_tasks.add_task(run_in_profile, profile_id, _update_cooccurrence_for_person, person_id)
    return {"status": "merged", "person_id": person_id}


@router.post("/ignored/{person_id}/add-cluster/{cluster_id}")
async def add_cluster_to_ignored_person(person_id: int, cluster_id: int):
    async with get_db() as db:
        existing = await (
            await db.execute(
                "SELECT id FROM persons WHERE id=? AND is_merged=0 AND is_ignored=1",
                (person_id,),
            )
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Ignored person not found")

        cluster = await (
            await db.execute(
                "SELECT id FROM clusters WHERE id=?",
                (cluster_id,),
            )
        ).fetchone()
        if not cluster or await get_current_person_id_for_cluster(db, cluster_id) is not None:
            raise HTTPException(status_code=404, detail="Cluster not found or already assigned")

        await _assign_cluster_to_ignored_person(db, person_id, cluster_id)

    return {"status": "ignored", "person_id": person_id, "cluster_id": cluster_id}


@router.post("/{person_id}/set-portrait/{face_id}")
async def set_portrait_face(person_id: int, face_id: int):
    """
    Pin a specific face crop as the representative thumbnail for a person.
    The chosen face must already belong to this person.
    """
    async with get_db() as db:
        row = await (
            await db.execute(
                """
                SELECT f.id
                FROM faces f
                LEFT JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                LEFT JOIN persons p ON p.person_guid = cpc.person_guid AND p.is_merged=0
                WHERE f.id=? AND p.id=?
                """,
                (face_id, person_id),
            )
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail="Face not found or does not belong to this person.",
            )
        await db.execute(
            "UPDATE persons SET portrait_face_id=? WHERE id=?",
            (face_id, person_id),
        )
    return {"status": "ok", "person_id": person_id, "portrait_face_id": face_id}


# ---------------------------------------------------------------------------
# Co-occurrence: "frequently appears with" + connection graph
# ---------------------------------------------------------------------------

@router.get("/shared-media")
async def shared_media_for_nodes(
    node_a_type: str,
    node_a_id: int,
    node_b_type: str,
    node_b_id: int,
    limit: int = 100,
):
    """
    Return media files where both graph nodes (person or cluster) appear in
    the same photo.  Used by the Connections graph edge-click shared-photos view.

    node_type ∈ {'person', 'cluster'}
    """
    if node_a_type not in ("person", "cluster") or node_b_type not in ("person", "cluster"):
        raise HTTPException(status_code=400, detail="node_type must be 'person' or 'cluster'")
    limit = min(max(limit, 1), 500)

    if node_a_type == "person":
        join_a = "JOIN faces fa ON fa.media_file_id = mf.id AND fa.person_id = ?"
    else:
        join_a = "JOIN faces fa ON fa.media_file_id = mf.id AND fa.cluster_id = ? AND fa.person_id IS NULL"

    if node_b_type == "person":
        join_b = "JOIN faces fb ON fb.media_file_id = mf.id AND fb.person_id = ?"
    else:
        join_b = "JOIN faces fb ON fb.media_file_id = mf.id AND fb.cluster_id = ? AND fb.person_id IS NULL"

    sql = f"""
        SELECT DISTINCT mf.*
        FROM media_files mf
        {join_a}
        {join_b}
        WHERE mf.removed_from_app = 0
        ORDER BY mf.date_taken DESC, mf.id DESC
        LIMIT ?
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(sql, (node_a_id, node_b_id, limit))
    return [dict(r) for r in rows]


@router.get("/{person_id}/connections-graph")
async def connections_graph(person_id: int, depth: int = 2):
    """
    Return a social graph of persons and unnamed clusters that co-appear in
    photos alongside person_id, up to `depth` hops (max 2).

    Nodes: named persons + unnamed clusters (not ignored).
    Edges: shared photo count between each pair.
    The graph is used for the Connections visualisation in the People tab.
    """
    depth = min(max(depth, 1), 2)
    async with get_db() as db:
        # ── Verify center ──────────────────────────────────────────────────
        center = await (await db.execute(
            "SELECT id, name, photo_count FROM persons "
            "WHERE id=? AND is_merged=0 AND is_ignored=0",
            (person_id,),
        )).fetchone()
        if not center:
            raise HTTPException(status_code=404, detail="Person not found")

        center_thumb = await (await db.execute(
            """
            SELECT COALESCE(pf.thumbnail_path,
                           (SELECT f.thumbnail_path FROM faces f
                            WHERE f.person_id=? AND f.thumbnail_path IS NOT NULL LIMIT 1)
                  ) AS thumbnail_path
            FROM persons p
            LEFT JOIN faces pf ON pf.id = p.portrait_face_id
            WHERE p.id=?
            """,
            (person_id, person_id),
        )).fetchone()

        center_nid = f"p_{person_id}"
        nodes: dict[str, dict] = {
            center_nid: {
                "id": center_nid,
                "type": "person",
                "raw_id": person_id,
                "name": center["name"],
                "photo_count": center["photo_count"],
                "thumbnail": center_thumb["thumbnail_path"] if center_thumb else None,
                "depth": 0,
            }
        }
        edges: dict[tuple, int] = {}

        # ── Depth-1 named: person_cooccurrence table ───────────────────────
        named_d1 = await db.execute_fetchall("""
            SELECT
                p.id,
                p.name,
                p.photo_count,
                pc.count AS shared_photos,
                COALESCE(pf.thumbnail_path, MIN(f.thumbnail_path)) AS thumbnail
            FROM person_cooccurrence pc
            JOIN persons p
              ON p.id = CASE
                  WHEN pc.person_a_id = ? THEN pc.person_b_id
                  ELSE pc.person_a_id
                END
            LEFT JOIN faces f
              ON f.person_id = p.id AND f.thumbnail_path IS NOT NULL
            LEFT JOIN faces pf ON pf.id = p.portrait_face_id
            WHERE (pc.person_a_id = ? OR pc.person_b_id = ?)
              AND p.is_merged  = 0
              AND p.is_ignored = 0
            GROUP BY p.id
            ORDER BY pc.count DESC
            LIMIT 15
        """, (person_id, person_id, person_id))

        for row in named_d1:
            nid = f"p_{row['id']}"
            nodes[nid] = {
                "id": nid,
                "type": "person",
                "raw_id": row["id"],
                "name": row["name"],
                "photo_count": row["photo_count"],
                "thumbnail": row["thumbnail"],
                "depth": 1,
            }
            eid = tuple(sorted([center_nid, nid]))
            edges[eid] = row["shared_photos"]

        # ── Depth-1 unnamed: clusters co-appearing in the same photos ──────
        unnamed_d1 = await db.execute_fetchall("""
            SELECT
                f2.cluster_id,
                COUNT(DISTINCT f2.media_file_id) AS shared_photos,
                MIN(f2.thumbnail_path)            AS thumbnail,
                c.member_count
            FROM faces f1
            JOIN faces f2
              ON  f2.media_file_id = f1.media_file_id
              AND f2.cluster_id IS NOT NULL
              AND f2.person_id   IS NULL
            JOIN clusters c ON c.id = f2.cluster_id
            WHERE f1.person_id = ?
            GROUP BY f2.cluster_id
            ORDER BY shared_photos DESC
        """, (person_id,))

        for row in unnamed_d1:
            nid = f"c_{row['cluster_id']}"
            if nid not in nodes:
                nodes[nid] = {
                    "id": nid,
                    "type": "cluster",
                    "raw_id": row["cluster_id"],
                    "name": None,
                    "photo_count": row["member_count"],
                    "thumbnail": row["thumbnail"],
                    "depth": 1,
                }
                eid = tuple(sorted([center_nid, nid]))
                edges[eid] = row["shared_photos"]

        # ── Depth-2: connections of depth-1 named persons ──────────────────
        d1_person_ids = [
            nodes[nid]["raw_id"]
            for nid in nodes
            if nid.startswith("p_") and nid != center_nid
        ]
        if depth >= 2 and d1_person_ids:
            ph = ",".join("?" * len(d1_person_ids))
            # Fetch all cooccurrence edges touching any depth-1 person
            cooc2 = await db.execute_fetchall(f"""
                SELECT person_a_id, person_b_id, count AS shared_photos
                FROM person_cooccurrence
                WHERE (person_a_id IN ({ph}) OR person_b_id IN ({ph}))
            """, (*d1_person_ids, *d1_person_ids))

            d1_set = set(d1_person_ids)
            new_person_ids: set[int] = set()
            for row in cooc2:
                pa, pb = row["person_a_id"], row["person_b_id"]
                # Determine which side is the depth-1 source
                src_raw = pa if pa in d1_set else pb
                tgt_raw = pb if pa in d1_set else pa
                if tgt_raw == person_id:
                    continue  # edge to center already stored
                src_nid = f"p_{src_raw}"
                tgt_nid = f"p_{tgt_raw}"
                eid = tuple(sorted([src_nid, tgt_nid]))
                edges[eid] = max(edges.get(eid, 0), row["shared_photos"])
                if tgt_nid not in nodes:
                    new_person_ids.add(tgt_raw)

            # Fetch person info for discovered depth-2 nodes
            if new_person_ids:
                ph2 = ",".join("?" * len(new_person_ids))
                new_ids_list = list(new_person_ids)
                p2_rows = await db.execute_fetchall(f"""
                    SELECT p.id, p.name, p.photo_count,
                           COALESCE(pf.thumbnail_path, MIN(f.thumbnail_path)) AS thumbnail
                    FROM persons p
                    LEFT JOIN faces f
                      ON f.person_id = p.id AND f.thumbnail_path IS NOT NULL
                    LEFT JOIN faces pf ON pf.id = p.portrait_face_id
                    WHERE p.id IN ({ph2})
                      AND p.is_merged  = 0
                      AND p.is_ignored = 0
                    GROUP BY p.id
                """, new_ids_list)
                for row in p2_rows:
                    nid = f"p_{row['id']}"
                    if nid not in nodes:
                        nodes[nid] = {
                            "id": nid,
                            "type": "person",
                            "raw_id": row["id"],
                            "name": row["name"],
                            "photo_count": row["photo_count"],
                            "thumbnail": row["thumbnail"],
                            "depth": 2,
                        }

    return {
        "center_id": center_nid,
        "nodes": list(nodes.values()),
        "edges": [
            {"source": e[0], "target": e[1], "weight": w}
            for e, w in edges.items()
            if e[0] in nodes and e[1] in nodes
        ],
    }


@router.get("/{person_id}/frequently-with")
async def frequently_with(person_id: int, limit: int = 10):
    """
    Return up to `limit` named persons that most often appear in the same
    photos as `person_id`, ordered by shared photo count descending.

    Uses the person_cooccurrence table which is rebuilt after every ingest /
    reprocess cycle.  Returns an empty list if the table has no edges yet.
    """
    async with get_db() as db:
        person = await (
            await db.execute(
                "SELECT id FROM persons WHERE id=? AND is_merged=0 AND is_ignored=0",
                (person_id,),
            )
        ).fetchone()
        if not person:
            raise HTTPException(status_code=404, detail="Person not found")

        rows = await db.execute_fetchall("""
            SELECT
                p.id,
                p.name,
                pc.count          AS shared_photos,
                pc.last_seen_at,
                COALESCE(pf.thumbnail_path, MIN(f.thumbnail_path)) AS representative_thumbnail
            FROM person_cooccurrence pc
            JOIN persons p
              ON  p.id = CASE
                    WHEN pc.person_a_id = ? THEN pc.person_b_id
                    ELSE pc.person_a_id
                  END
            LEFT JOIN faces f ON f.person_id = p.id
            LEFT JOIN faces pf ON pf.id = p.portrait_face_id
            WHERE (pc.person_a_id = ? OR pc.person_b_id = ?)
              AND p.name IS NOT NULL
              AND p.is_merged  = 0
              AND p.is_ignored = 0
            GROUP BY p.id
            ORDER BY pc.count DESC
            LIMIT ?
        """, (person_id, person_id, person_id, limit))

    return [dict(r) for r in rows]


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
            JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
            JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
            JOIN persons p ON p.person_guid = cpc.person_guid
            WHERE p.id = ?
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
            LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = c.cluster_guid
            LEFT JOIN persons p ON p.person_guid = cpc.person_guid AND p.is_merged = 0 AND p.is_ignored = 0
            WHERE p.id IS NULL
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
                   (
                       SELECT MIN(f2.thumbnail_path)
                       FROM faces f2
                       JOIN v_face_cluster_current fcc2 ON fcc2.face_guid = f2.face_guid
                       JOIN v_cluster_person_current cpc2 ON cpc2.cluster_guid = fcc2.cluster_guid
                       WHERE cpc2.person_guid = p.person_guid
                   ) AS thumbnail
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
            LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = c.cluster_guid
            LEFT JOIN persons p ON p.person_guid = cpc.person_guid AND p.is_merged = 0 AND p.is_ignored = 0
            WHERE p.id IS NULL
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
                JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                JOIN persons p ON p.person_guid = cpc.person_guid
                WHERE p.id = ?
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
                    await db.execute(UPDATE_CLUSTER_PERSON_SQL, (person_id, cid))
                    await db.execute(UPDATE_CLUSTER_FACES_PERSON_SQL, (person_id, cid))
                    await link_cluster_to_person(
                        db,
                        cluster_id=cid,
                        person_id=person_id,
                        source="auto_merge_similarity",
                        actor="api.find_similar_all",
                    )
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
            SELECT COUNT(DISTINCT f.media_file_id)
            FROM faces f
            JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
            JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
            JOIN persons p ON p.person_guid = cpc.person_guid
            WHERE p.id = ?
        )
        WHERE id = ?
    """, (person_id, person_id))


@router.get("/{person_id}/faces")
async def get_person_faces(
    person_id: int,
    limit: int | None = None,
    offset: int = 0,
    sort_by: str = "detection_conf",
    sort_dir: str = "desc",
):
    """All face thumbnails assigned to a person, for review / false-positive removal."""
    offset = max(0, int(offset or 0))
    sort_col = {
        "detection_conf": "f.detection_conf",
        "date_taken": "mf.date_taken",
        "id": "f.id",
    }.get((sort_by or "").lower(), "f.detection_conf")
    sort_direction = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"

    order_sql = f"ORDER BY {sort_col} {sort_direction}, f.id DESC"

    async with get_db() as db:
        if limit is not None and limit > 0:
            rows = await db.execute_fetchall(f"""
                SELECT f.id, f.thumbnail_path, f.detection_conf,
                       f.media_file_id, mf.file_path, mf.date_taken
                FROM faces f
                JOIN media_files mf ON mf.id = f.media_file_id
                JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                JOIN persons p ON p.person_guid = cpc.person_guid
                WHERE p.id = ?
                {order_sql}
                LIMIT ? OFFSET ?
            """, (person_id, limit, offset))
        else:
            rows = await db.execute_fetchall(f"""
                SELECT f.id, f.thumbnail_path, f.detection_conf,
                       f.media_file_id, mf.file_path, mf.date_taken
                FROM faces f
                JOIN media_files mf ON mf.id = f.media_file_id
                JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                JOIN persons p ON p.person_guid = cpc.person_guid
                WHERE p.id = ?
                {order_sql}
            """, (person_id,))
    return [dict(r) for r in rows]
