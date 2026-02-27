"""VIP API — Faces routes (thumbnail serving + face management)."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path

from backend.database.db import get_db

router = APIRouter()


@router.get("/{face_id}/thumbnail")
async def get_face_thumbnail(face_id: int):
    """Serve the face crop thumbnail JPEG."""
    async with get_db() as db:
        row = await (
            await db.execute("SELECT thumbnail_path FROM faces WHERE id=?", (face_id,))
        ).fetchone()

    if not row or not row["thumbnail_path"]:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    path = Path(row["thumbnail_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file missing on disk")

    return FileResponse(path, media_type="image/jpeg")


@router.get("/cluster/{cluster_id}")
async def get_cluster_faces(cluster_id: int, limit: int = 20):
    """Return representative face thumbnails for a cluster."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT f.id, f.thumbnail_path, f.detection_conf, f.person_id,
                   mf.file_path, mf.date_taken
            FROM faces f
            JOIN media_files mf ON mf.id = f.media_file_id
            WHERE f.cluster_id = ?
            ORDER BY f.detection_conf DESC
            LIMIT ?
        """, (cluster_id, limit))

    return [dict(r) for r in rows]


@router.delete("/{face_id}/from-cluster")
async def remove_face_from_cluster(face_id: int):
    """Detach a face from its cluster (user flagged it as incorrect)."""
    async with get_db() as db:
        await db.execute(
            "UPDATE faces SET cluster_id=NULL, person_id=NULL WHERE id=?",
            (face_id,),
        )
    return {"status": "removed", "face_id": face_id}


@router.delete("/{face_id}/from-person")
async def remove_face_from_person(face_id: int):
    """
    Remove a face from its person assignment (false positive correction).
    The face is detached from person + cluster so it re-enters the unassigned pool.
    The media file is re-queued for writeback so the person is removed from EXIF.
    """
    async with get_db() as db:
        # Find what person this face belongs to (for writeback re-queue)
        row = await (
            await db.execute(
                "SELECT person_id, media_file_id FROM faces WHERE id=?", (face_id,)
            )
        ).fetchone()

        if not row:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Face not found")

        await db.execute(
            "UPDATE faces SET cluster_id=NULL, person_id=NULL WHERE id=?", (face_id,)
        )

        # Re-queue the media file so writeback rewrites EXIF without this person
        if row["media_file_id"]:
            await db.execute("""
                INSERT OR REPLACE INTO writeback_queue (media_file_id, status, queued_at)
                VALUES (?, 'pending', datetime('now'))
            """, (row["media_file_id"],))

    return {"status": "removed", "face_id": face_id}
