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
_SUGGEST_THRESHOLD = 0.55

router = APIRouter()


class NamePersonRequest(BaseModel):
    name: str


class MergeRequest(BaseModel):
    into_person_id: int     # merge source → target


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
                       WHERE f2.person_id = p.id AND wq.status = 'done'
                   ) THEN 1 ELSE 0 END                        AS name_written
            FROM persons p
            LEFT JOIN faces f ON f.person_id = p.id
            WHERE p.is_merged = 0
            GROUP BY p.id
            ORDER BY photo_count DESC
        """)
    return [dict(r) for r in rows]


@router.get("/unnamed")
async def list_unnamed_clusters():
    """Clusters not yet assigned to a named person."""
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

        # Queue all this person's photos for writeback
        await db.execute("""
            INSERT OR IGNORE INTO writeback_queue (media_file_id)
            SELECT DISTINCT f.media_file_id
            FROM faces f
            WHERE f.person_id = ?
        """, (person_id,))

        # Persist centroid so this person is recognisable in future scans
        await update_person_centroid(db, person_id)

    return {"status": "ok", "name": req.name}


@router.post("/merge")
async def merge_persons(req: MergeRequest, source_id: int):
    """Merge two persons (same person, different clusters)."""
    async with get_db() as db:
        # Re-assign all faces from source → target
        await db.execute(
            "UPDATE faces SET person_id=? WHERE person_id=?",
            (req.into_person_id, source_id),
        )
        # Mark source as merged
        await db.execute("""
            UPDATE persons SET is_merged=1, merged_into_id=? WHERE id=?
        """, (req.into_person_id, source_id))
        # Sync stored photo_count on target
        await _sync_photo_count(db, req.into_person_id)
        # Refresh centroid to include faces from the merged source
        await update_person_centroid(db, req.into_person_id)

    return {"status": "merged", "into": req.into_person_id}


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
            INSERT OR IGNORE INTO writeback_queue (media_file_id)
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
            INSERT OR IGNORE INTO writeback_queue (media_file_id)
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
