"""VIP API — Media routes."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path

from backend.database.db import get_db
from backend.database.models import MediaFile

router = APIRouter()


@router.get("", response_model=list[MediaFile])
async def list_media(limit: int = 50, offset: int = 0, state: str | None = None):
    async with get_db() as db:
        if state:
            rows = await db.execute_fetchall(
                "SELECT * FROM media_files WHERE ingest_state=? ORDER BY date_taken DESC LIMIT ? OFFSET ?",
                (state, limit, offset),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM media_files ORDER BY date_taken DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
    return [dict(r) for r in rows]


@router.get("/{media_id}", response_model=MediaFile)
async def get_media(media_id: int):
    async with get_db() as db:
        row = await (
            await db.execute("SELECT * FROM media_files WHERE id=?", (media_id,))
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Media file not found")
    return dict(row)


@router.get("/{media_id}/preview")
async def serve_preview(media_id: int):
    """Serve the extracted JPEG preview for a media file."""
    async with get_db() as db:
        row = await (
            await db.execute("SELECT file_path FROM media_files WHERE id=?", (media_id,))
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Media file not found")

    from backend.config import settings
    stem = Path(row["file_path"]).stem
    import hashlib
    suffix = hashlib.md5(row["file_path"].encode()).hexdigest()[:8]
    preview = settings.preview_dir / f"{stem}_{suffix}.jpg"

    if not preview.exists():
        raise HTTPException(
            status_code=404,
            detail="Preview not available. File may need processing or may be offloaded.",
        )

    return FileResponse(preview, media_type="image/jpeg")
