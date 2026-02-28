"""VIP API — Media routes."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from backend.config import settings
from backend.database.db import get_db
from backend.database.models import MediaFile

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _thumb_path(media_id: int) -> Path:
    return settings.photo_thumbs_dir / f"{media_id}.jpg"


def _make_thumbnail_sync(src: Path, dst: Path) -> None:
    """Resize a JPEG to a 600-px wide thumbnail. Requires Pillow."""
    from PIL import Image
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        img.thumbnail((600, 800), Image.LANCZOS)
        img.save(dst, "JPEG", quality=85, optimize=True)


def _build_filter_clauses(
    person_id: int | None,
    tag_category: str | None,
    tag_label: str | None,
    state: str | None,
) -> tuple[list[str], list[str], list]:
    joins: list[str] = []
    conditions: list[str] = []
    params: list = []

    if person_id is not None:
        joins.append("JOIN faces _f ON _f.media_file_id = mf.id")
        conditions.append("_f.person_id = ?")
        params.append(person_id)

    if tag_category and tag_label:
        joins.append("JOIN media_tags _mt ON _mt.media_file_id = mf.id")
        conditions.append("_mt.category = ? AND _mt.label = ?")
        params.extend([tag_category, tag_label])
    elif tag_category:
        joins.append("JOIN media_tags _mt ON _mt.media_file_id = mf.id")
        conditions.append("_mt.category = ?")
        params.append(tag_category)

    if state:
        conditions.append("mf.ingest_state = ?")
        params.append(state)

    return joins, conditions, params


# ---------------------------------------------------------------------------
# Count — declared BEFORE /{media_id} to avoid route shadowing
# ---------------------------------------------------------------------------

@router.get("/count")
async def count_media(
    person_id: int | None = None,
    tag_category: str | None = None,
    tag_label: str | None = None,
    state: str | None = None,
):
    """Return total count of media files matching the given filters."""
    joins, conditions, params = _build_filter_clauses(
        person_id=person_id,
        tag_category=tag_category,
        tag_label=tag_label,
        state=state,
    )
    sql = f"""
        SELECT COUNT(DISTINCT mf.id) AS n
        FROM media_files mf
        {' '.join(joins)}
        {('WHERE ' + ' AND '.join(conditions)) if conditions else ''}
    """
    async with get_db() as db:
        row = await (await db.execute(sql, params)).fetchone()
    return {"count": row["n"] if row else 0}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@router.get("")
async def list_media(
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    state: str | None = None,
    person_id: int | None = None,
    tag_category: str | None = None,
    tag_label: str | None = None,
):
    """
    List media files. All filters are combinable:
      - state       — ingest state (scanned / embedded / clustered / tagged)
      - person_id   — photos that contain this person (via faces table)
      - tag_category + tag_label — photos with a specific ML tag
    """
    joins, conditions, params = _build_filter_clauses(
        person_id=person_id,
        tag_category=tag_category,
        tag_label=tag_label,
        state=state,
    )
    params.extend([limit, offset])
    sql = f"""
        SELECT DISTINCT mf.*
        FROM media_files mf
        {' '.join(joins)}
        {('WHERE ' + ' AND '.join(conditions)) if conditions else ''}
        ORDER BY mf.date_taken DESC
        LIMIT ? OFFSET ?
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(sql, params)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Single file
# ---------------------------------------------------------------------------

@router.get("/{media_id}", response_model=MediaFile)
async def get_media(media_id: int):
    async with get_db() as db:
        row = await (
            await db.execute("SELECT * FROM media_files WHERE id=?", (media_id,))
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Media file not found")
    return dict(row)


# ---------------------------------------------------------------------------
# Photo thumbnail (600-px wide, permanently cached)
# ---------------------------------------------------------------------------

@router.get("/{media_id}/thumbnail")
async def serve_photo_thumbnail(media_id: int):
    """
    Serve a 600-px wide JPEG thumbnail for a media file.

    Priority:
    1. Cached thumbnail (photo_thumbs/{media_id}.jpg) — generated during Phase 2
    2. On-demand re-extraction from the RAW file → resize → cache → serve
    """
    thumb = _thumb_path(media_id)
    if thumb.exists():
        return FileResponse(thumb, media_type="image/jpeg")

    # Fallback: re-extract (requires file to be locally present)
    async with get_db() as db:
        row = await (
            await db.execute("SELECT file_path FROM media_files WHERE id=?", (media_id,))
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Media file not found")

    path = Path(row["file_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not on disk — may be offloaded to iCloud.")

    from backend.scanner.preview_extractor import extract_preview, delete_preview

    preview = await extract_preview(path)
    if preview is None:
        raise HTTPException(status_code=404, detail="Could not extract preview from file.")

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _make_thumbnail_sync, preview, thumb)
    finally:
        await delete_preview(preview)

    if thumb.exists():
        return FileResponse(thumb, media_type="image/jpeg")
    raise HTTPException(status_code=500, detail="Thumbnail generation failed.")


# ---------------------------------------------------------------------------
# Full-size preview (pipeline-temporary — use /thumbnail for the UI)
# ---------------------------------------------------------------------------

@router.get("/{media_id}/preview")
async def serve_preview(media_id: int):
    """Serve the extracted JPEG preview (only present during pipeline runs)."""
    async with get_db() as db:
        row = await (
            await db.execute("SELECT file_path FROM media_files WHERE id=?", (media_id,))
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Media file not found")

    stem = Path(row["file_path"]).stem
    suffix = hashlib.md5(row["file_path"].encode()).hexdigest()[:8]
    preview = settings.preview_dir / f"{stem}_{suffix}.jpg"

    if not preview.exists():
        raise HTTPException(status_code=404, detail="Preview not available. Try /thumbnail instead.")
    return FileResponse(preview, media_type="image/jpeg")
