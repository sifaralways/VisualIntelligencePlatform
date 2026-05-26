"""VIP API — Faces routes (thumbnail serving + face management)."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pathlib import Path

from backend.config import settings
from backend.database.db import get_db
from backend.pipeline.centroid import update_person_centroid

router = APIRouter()


async def _requeue_as_singleton(db, face_id: int) -> None:
    """
    Create a new 1-member cluster from the face's own embedding and assign
    the face to it, so the face immediately reappears in the unnamed-faces
    list rather than disappearing until the next pipeline run.

    If the face has no stored embedding (edge case), leaves cluster_id NULL —
    the next clustering phase will handle it.
    """
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
        new_cluster_id = cursor.lastrowid
        await db.execute(
            "UPDATE faces SET cluster_id=? WHERE id=?", (new_cluster_id, face_id)
        )


@router.get("/{face_id}/thumbnail")
async def get_face_thumbnail(face_id: int):
    """Serve the face crop thumbnail JPEG."""
    async with get_db() as db:
        row = await (
            await db.execute("SELECT thumbnail_path FROM faces WHERE id=?", (face_id,))
        ).fetchone()

    if not row or not row["thumbnail_path"]:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    raw_path = Path(row["thumbnail_path"])

    # Profile migration moved thumbnails under per-profile directories.
    # Legacy DB rows may still point at the old shared root path.
    path = raw_path if raw_path.exists() else (settings.thumbnail_dir / raw_path.name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file missing on disk")

    # Read into memory so Content-Length comes from actual bytes read, not a
    # potentially stale stat() on a NAS/SMB mount.
    return Response(content=path.read_bytes(), media_type="image/jpeg")


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


@router.get("/media/{media_id}")
async def get_faces_for_media(
    media_id: int,
    include_ignored: bool = Query(False, alias="include_ignored"),
):
    """Return all faces detected in a specific media file, with person names."""
    import json as _json
    async with get_db() as db:
        if include_ignored:
            # Return all faces including those assigned to ignored persons.
            # is_ignored=1 faces are returned with person_name=NULL (they have no real name).
            rows = await db.execute_fetchall("""
                SELECT f.id, f.thumbnail_path, f.detection_conf,
                       f.cluster_id, f.person_id,
                       CASE WHEN p.is_ignored = 0 THEN p.name ELSE NULL END AS person_name,
                       f.face_attributes,
                       COALESCE(p.is_ignored, 0) AS is_ignored
                FROM faces f
                LEFT JOIN persons p ON p.id = f.person_id AND p.is_merged = 0
                WHERE f.media_file_id = ?
                ORDER BY f.detection_conf DESC
            """, (media_id,))
        else:
            rows = await db.execute_fetchall("""
                SELECT f.id, f.thumbnail_path, f.detection_conf,
                       f.cluster_id, f.person_id, p.name AS person_name,
                       f.face_attributes,
                       0 AS is_ignored
                FROM faces f
                LEFT JOIN persons p ON p.id = f.person_id AND p.is_merged = 0 AND p.is_ignored = 0
                WHERE f.media_file_id = ?
                  AND (f.person_id IS NULL OR p.id IS NOT NULL)
                ORDER BY f.detection_conf DESC
            """, (media_id,))
    result = []
    for r in rows:
        d = dict(r)
        sharpness: float | None = None
        if d.get("face_attributes"):
            try:
                attrs = _json.loads(d["face_attributes"])
                raw = attrs.get("Quality", {}).get("Sharpness")
                if raw is not None:
                    sharpness = round(float(raw), 1)
            except Exception:
                pass
        d["sharpness"] = sharpness
        d["is_ignored"] = bool(d["is_ignored"])
        del d["face_attributes"]
        result.append(d)
    return result


@router.delete("/{face_id}/from-cluster")
async def remove_face_from_cluster(face_id: int):
    """Detach a face from its cluster (user flagged it as incorrect).

    The face is immediately placed in a new 1-member cluster so it reappears
    in the unnamed faces list rather than disappearing until the next pipeline.
    """
    async with get_db() as db:
        await db.execute(
            "UPDATE faces SET cluster_id=NULL, person_id=NULL WHERE id=?",
            (face_id,),
        )
        await _requeue_as_singleton(db, face_id)
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

        # Recompute the centroid so it no longer includes this face's embedding.
        # Without this the stored centroid stays stale and future similarity
        # comparisons remain biased toward the ejected face.
        if row["person_id"]:
            await update_person_centroid(db, row["person_id"])

        # Place the face in a new 1-member cluster so it immediately reappears
        # in the unnamed-faces list rather than being lost until re-clustering.
        await _requeue_as_singleton(db, face_id)

    return {"status": "removed", "face_id": face_id}
