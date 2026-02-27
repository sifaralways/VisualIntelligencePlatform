"""VIP API — Persons routes."""

from __future__ import annotations

import uuid as _uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database.db import get_db
from backend.database.models import Person

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
                   COUNT(DISTINCT f.media_file_id) as photo_count,
                   MIN(f.thumbnail_path) as representative_thumbnail
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

        # Update photo count on target
        await db.execute("""
            UPDATE persons SET photo_count=(
                SELECT COUNT(DISTINCT media_file_id) FROM faces WHERE person_id=?
            ) WHERE id=?
        """, (req.into_person_id, req.into_person_id))

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

        # Queue photos for writeback
        await db.execute("""
            INSERT OR IGNORE INTO writeback_queue (media_file_id)
            SELECT DISTINCT media_file_id FROM faces WHERE cluster_id=?
        """, (cluster_id,))

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

        # Queue photos for writeback
        await db.execute("""
            INSERT OR IGNORE INTO writeback_queue (media_file_id)
            SELECT DISTINCT media_file_id FROM faces WHERE cluster_id=?
        """, (cluster_id,))

    return {"status": "merged", "person_id": person_id}
