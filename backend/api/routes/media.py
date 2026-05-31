"""VIP API — Media routes."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from backend.config import settings
from backend.database.db import get_db
from backend.database.models import MediaFile
from backend.pipeline.centroid import update_person_centroid

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _thumb_path(media_id: int) -> Path:
    return settings.photo_thumbs_dir / f"{media_id}.jpg"


def _make_thumbnail_sync(src: Path, dst: Path) -> None:
    """Resize a JPEG to a 600-px wide thumbnail. Requires Pillow."""
    from PIL import Image, ImageOps
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        # Apply EXIF orientation before resizing so portrait/landscape is correct.
        img = ImageOps.exif_transpose(img)
        img.thumbnail((600, 800), Image.LANCZOS)
        img.save(dst, "JPEG", quality=85, optimize=True)


def _build_filter_clauses(
    person_id: int | None,
    tag_category: str | None,
    tag_label: str | None,
    state: str | None,
    folder_id: int | None = None,
    cluster_id: int | None = None,
    path_prefix: str | None = None,
) -> tuple[list[str], list[str], list]:
    joins: list[str] = []
    # Always hide soft-removed photos from every query
    conditions: list[str] = ["mf.removed_from_app = 0"]
    params: list = []

    if path_prefix is not None:
        # Subfolder click — filter by a literal path prefix.
        # Append '/' so that /Volumes/Photos/2023 doesn't accidentally match
        # /Volumes/Photos/20230101.jpg.
        conditions.append("mf.file_path LIKE ?")
        params.append(path_prefix + "/%")
    elif folder_id is not None:
        # Use a subquery — avoids a cross-product JOIN and handles nested paths cleanly
        conditions.append(
            "mf.file_path LIKE (SELECT folder_path || '/%' FROM scan_state WHERE id=?)"
        )
        params.append(folder_id)

    # Faces-table join (shared by person_id and cluster_id filters)
    face_conditions: list[str] = []
    if person_id is not None:
        face_conditions.append("_f.person_id = ?")
        params.append(person_id)
    if cluster_id is not None:
        face_conditions.append("_f.cluster_id = ?")
        params.append(cluster_id)
    if face_conditions:
        joins.append("JOIN faces _f ON _f.media_file_id = mf.id")
        conditions.extend(face_conditions)

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
    folder_id: int | None = None,
    cluster_id: int | None = None,
    path_prefix: str | None = None,
):
    """Return total count of media files matching the given filters."""
    joins, conditions, params = _build_filter_clauses(
        person_id=person_id,
        tag_category=tag_category,
        tag_label=tag_label,
        state=state,
        folder_id=folder_id,
        cluster_id=cluster_id,
        path_prefix=path_prefix,
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
    folder_id: int | None = None,
    cluster_id: int | None = None,
    path_prefix: str | None = None,
):
    """
    List media files. All filters are combinable:
      - state        — ingest state (scanned / embedded / clustered / tagged)
      - person_id    — photos that contain this person (via faces table)
      - tag_category + tag_label — photos with a specific ML tag
      - folder_id    — photos from a specific scanned folder
      - cluster_id   — photos that contain a face from this unnamed cluster
      - path_prefix  — photos whose file_path starts with this directory path
    """
    joins, conditions, params = _build_filter_clauses(
        person_id=person_id,
        tag_category=tag_category,
        tag_label=tag_label,
        state=state,
        folder_id=folder_id,
        cluster_id=cluster_id,
        path_prefix=path_prefix,
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
# Quality issues
# ---------------------------------------------------------------------------

@router.get("/quality")
async def quality_issues(issue: str = Query("all", pattern="^(blurry|closed_eyes|all)$")):
    """Return media files flagged with quality issues.

    issue: 'blurry' | 'closed_eyes' | 'all'
    """
    if issue == "blurry":
        condition = "mf.is_blurry = 1"
    elif issue == "closed_eyes":
        condition = "mf.has_closed_eyes = 1"
    else:
        condition = "(mf.is_blurry = 1 OR mf.has_closed_eyes = 1)"

    sql = f"""
        SELECT
            mf.id, mf.file_path, mf.date_taken,
            mf.blur_score, mf.is_blurry, mf.long_exposure,
            mf.has_closed_eyes, mf.width, mf.height
        FROM media_files mf
        WHERE {condition}
          AND mf.removed_from_app = 0
        ORDER BY mf.date_taken DESC, mf.id DESC
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(sql, [])
    results = []
    for r in rows:
        item = dict(r)
        thumb = _thumb_path(item["id"])
        item["thumbnail_url"] = f"/api/media/{item['id']}/thumbnail" if thumb.exists() else None
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# Soft-remove from app (preserves DB row + UUID for future re-scan)
# ---------------------------------------------------------------------------

class RemoveFromAppRequest(BaseModel):
    media_ids: List[int]
    force: bool = False  # True = skip writeback warning


@router.post("/remove-from-app")
async def remove_from_app(body: RemoveFromAppRequest):
    """
    Soft-remove photos: sets removed_from_app=1 so they disappear from the UI
    but the DB row (including file_hash and vip_id) is preserved.

    If any of the selected photos have metadata assigned but not yet written to
    file (pending writeback), and force=False, returns a warning payload instead
    of removing. The client should prompt the user and re-call with force=True.
    """
    if not body.media_ids:
        return {"removed": 0}

    async with get_db() as db:
        if not body.force:
            placeholders = ",".join("?" * len(body.media_ids))
            unwritten = await db.execute_fetchall(
                f"""
                SELECT mf.id, mf.file_path
                FROM media_files mf
                JOIN writeback_queue wq ON wq.media_file_id = mf.id
                WHERE mf.id IN ({placeholders})
                  AND mf.removed_from_app = 0
                  AND wq.status = 'pending'
                """,
                body.media_ids,
            )
            if unwritten:
                return {
                    "status": "warning",
                    "unwritten_count": len(unwritten),
                    "unwritten_paths": [r["file_path"] for r in unwritten[:5]],
                }

        placeholders = ",".join("?" * len(body.media_ids))
        result = await db.execute(
            f"UPDATE media_files SET removed_from_app=1 WHERE id IN ({placeholders})",
            body.media_ids,
        )
        queue_result = await db.execute(
            f"DELETE FROM writeback_queue WHERE media_file_id IN ({placeholders})",
            body.media_ids,
        )
    return {
        "status": "ok",
        "removed": result.rowcount,
        "writeback_rows_deleted": queue_result.rowcount,
    }


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------

class BulkDeleteRequest(BaseModel):
    media_ids: List[int]


@router.delete("/bulk")
async def bulk_delete(body: BulkDeleteRequest):
    """Permanently delete one or more media files (photo thumbs, face thumbs, DB rows)."""
    if not body.media_ids:
        return {"deleted": 0}

    deleted = 0
    affected_person_ids: set[int] = set()

    async with get_db() as db:
        for media_id in body.media_ids:
            # Collect person_ids before deletion so we can refresh their centroids
            person_rows = await db.execute_fetchall(
                """
                SELECT DISTINCT p.id AS person_id
                FROM faces f
                JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                JOIN persons p ON p.person_guid = cpc.person_guid
                WHERE f.media_file_id=?
                """,
                (media_id,),
            )
            for pr in person_rows:
                affected_person_ids.add(pr["person_id"])

            # Fetch face ids so we can remove their thumbnails
            face_rows = await db.execute_fetchall(
                "SELECT id, thumbnail_path FROM faces WHERE media_file_id=?", (media_id,)
            )
            # Remove face thumbnails
            for fr in face_rows:
                if fr["thumbnail_path"]:
                    try:
                        Path(fr["thumbnail_path"]).unlink(missing_ok=True)
                    except Exception:
                        pass
            # Remove photo thumbnail
            try:
                _thumb_path(media_id).unlink(missing_ok=True)
            except Exception:
                pass
            # Remove DB row (faces + embeddings cascade via FK)
            result = await db.execute(
                "DELETE FROM media_files WHERE id=?", (media_id,)
            )
            if result.rowcount:
                deleted += 1

        # Refresh centroids for every person who lost faces — if all their photos
        # were deleted the centroid is set to NULL but the person record survives,
        # so their identity is retained and can be matched in future scans once
        # new photos with stored centroid vectors are available.
        for person_id in affected_person_ids:
            await update_person_centroid(db, person_id)

    return {"deleted": deleted}


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
        # Read into memory so Content-Length is derived from actual bytes, not
        # a potentially stale stat() on a NAS/SMB mount (avoids uvicorn's
        # "Response content shorter than Content-Length" RuntimeError).
        return Response(content=thumb.read_bytes(), media_type="image/jpeg")

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

    from PIL import UnidentifiedImageError

    from backend.scanner.preview_extractor import extract_preview, delete_preview

    preview = await extract_preview(path)
    if preview is None:
        raise HTTPException(status_code=404, detail="Could not extract preview from file.")

    try:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _make_thumbnail_sync, preview, thumb)
        except UnidentifiedImageError:
            # A stale/corrupt preview can linger from an interrupted pipeline run.
            preview.unlink(missing_ok=True)
            preview = await extract_preview(path)
            if preview is None:
                raise HTTPException(status_code=500, detail="Preview regeneration failed.")
            await loop.run_in_executor(None, _make_thumbnail_sync, preview, thumb)
    finally:
        await delete_preview(preview)

    if thumb.exists():
        return Response(content=thumb.read_bytes(), media_type="image/jpeg")
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
    return Response(content=preview.read_bytes(), media_type="image/jpeg")
