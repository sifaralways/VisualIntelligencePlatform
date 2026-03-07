"""VIP API — Folder management routes."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from backend.database.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# List scanned folders
# ---------------------------------------------------------------------------

@router.get("")
async def list_folders():
    """
    Return all folders that have been scanned, with live photo counts.

    active_count             — photos in that folder not yet soft-removed
    pending_writeback_count  — photos with unwritten metadata (status='pending')
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT
                ss.id,
                ss.folder_path,
                ss.last_scan_at,
                ss.file_count,
                ss.status,
                COUNT(DISTINCT mf.id)                                           AS active_count,
                COUNT(DISTINCT CASE WHEN wq.status='pending' THEN wq.media_file_id END)
                                                                                AS pending_writeback_count
            FROM scan_state ss
            LEFT JOIN media_files mf
                   ON mf.file_path LIKE ss.folder_path || '/%'
                  AND mf.removed_from_app = 0
            LEFT JOIN writeback_queue wq
                   ON wq.media_file_id = mf.id
            GROUP BY ss.id
            ORDER BY ss.last_scan_at DESC
            """
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Remove an entire folder from the app (soft-remove all its media)
# ---------------------------------------------------------------------------

@router.post("/{folder_id}/remove-from-app")
async def remove_folder_from_app(folder_id: int, force: bool = False):
    """
    Soft-remove all photos in a scanned folder.

    Sets removed_from_app=1 on every active media_file whose path is under
    the folder's root.  The DB rows (file_hash, vip_id…) are preserved so
    a rescan can re-use them.

    If force=False and there are pending writeback entries, returns a warning
    payload.  The client should confirm with the user and re-call with
    force=True.
    """
    async with get_db() as db:
        folder_row = await (
            await db.execute(
                "SELECT id, folder_path FROM scan_state WHERE id=?", (folder_id,)
            )
        ).fetchone()

    if not folder_row:
        raise HTTPException(status_code=404, detail="Folder not found")

    folder_path: str = folder_row["folder_path"]
    path_prefix = folder_path + "/%"

    async with get_db() as db:
        if not force:
            unwritten = await db.execute_fetchall(
                """
                SELECT mf.id, mf.file_path
                FROM media_files mf
                JOIN writeback_queue wq ON wq.media_file_id = mf.id
                WHERE mf.file_path LIKE ?
                  AND mf.removed_from_app = 0
                  AND wq.status = 'pending'
                """,
                (path_prefix,),
            )
            if unwritten:
                return {
                    "status": "warning",
                    "unwritten_count": len(unwritten),
                    "unwritten_paths": [r["file_path"] for r in unwritten[:5]],
                }

        result = await db.execute(
            "UPDATE media_files SET removed_from_app=1 WHERE file_path LIKE ? AND removed_from_app=0",
            (path_prefix,),
        )
        # Remove the scan_state row so the folder disappears from the sidebar
        await db.execute("DELETE FROM scan_state WHERE id=?", (folder_id,))

    # ── Delete derived files that will be recreated on next scan ─────────────
    # photo_thumbs/{media_id}.jpg  — UI grid thumbnail, recreated in Phase 2
    # previews/{stem}_{hash}.jpg   — temp preview, should already be gone but
    #                                may be stranded if a scan was interrupted
    # Face thumbnails (thumbnails/{face_id}.jpg) are intentionally kept:
    # they are reused by existing face/cluster/person rows on re-scan.
    from backend.config import settings

    async with get_db() as db:
        affected_media = await db.execute_fetchall(
            "SELECT id, file_path FROM media_files WHERE file_path LIKE ? AND removed_from_app=1",
            (path_prefix,),
        )

    deleted_thumbs = 0
    deleted_previews = 0
    for row in affected_media:
        media_id = row["id"]
        file_path = Path(row["file_path"])

        # Photo grid thumbnail
        photo_thumb = settings.photo_thumbs_dir / f"{media_id}.jpg"
        if photo_thumb.exists():
            photo_thumb.unlink(missing_ok=True)
            deleted_thumbs += 1

        # Stranded preview (name mirrors preview_extractor logic)
        import hashlib
        path_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
        preview = settings.preview_dir / f"{file_path.stem}_{path_hash}.jpg"
        if preview.exists():
            preview.unlink(missing_ok=True)
            deleted_previews += 1

    if deleted_thumbs or deleted_previews:
        logger.info(
            "Folder %s removed: deleted %d photo thumbnails, %d stranded previews",
            folder_path, deleted_thumbs, deleted_previews,
        )

    return {"status": "ok", "removed": result.rowcount,
            "deleted_photo_thumbs": deleted_thumbs,
            "deleted_previews": deleted_previews}
