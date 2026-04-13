"""
VIP API — Pipeline routes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from backend.database.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

# Simple in-memory pipeline state — sufficient for single-user local app
_pipeline_state: dict = {"status": "idle", "folder": None, "error": None}


class ScanRequest(BaseModel):
    folder: str
    force_reprocess: bool = False


class RescanRequest(BaseModel):
    force_retag: bool = False


@router.post("/scan")
async def start_scan(req: ScanRequest, background_tasks: BackgroundTasks):
    """Start the ingest pipeline on a given folder."""
    if _pipeline_state["status"] == "running":
        raise HTTPException(status_code=409, detail="Pipeline already running")

    folder = Path(req.folder).resolve()
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {folder}")

    if req.force_reprocess:
        async with get_db() as db:
            await db.execute("""
                UPDATE media_files SET needs_reprocess=1
                WHERE file_path LIKE ?
            """, (f"{folder}%",))

    # Upsert scan_state immediately so the sidebar shows the folder right away
    async with get_db() as db:
        await db.execute("""
            INSERT INTO scan_state (folder_path, status, last_scan_at, file_count)
            VALUES (?, 'scanning', datetime('now'), 0)
            ON CONFLICT(folder_path) DO UPDATE SET
                status       = 'scanning',
                last_scan_at = datetime('now')
        """, (str(folder),))

    _pipeline_state.update({"status": "running", "folder": str(folder), "error": None})
    background_tasks.add_task(_run_pipeline, str(folder))

    return {"status": "started", "folder": str(folder)}


@router.post("/rescan")
async def rescan_library(req: RescanRequest, background_tasks: BackgroundTasks):
    """Full library reprocess without a filesystem walk.

    Re-detects faces on photos not owned by a named person (applies any
    updated detection/clustering settings from the Admin page), re-clusters
    all unowned faces with the current HDBSCAN settings, runs auto-merge
    + always-ignore suppression, and refreshes quality signals.

    Named-person assignments are preserved.  Always-ignored faces are
    never re-surfaced.

    Pass force_retag=true to also re-run YOLO/CLIP object/scene/place
    detection on every photo (slower — avoids the tags_done skip).
    """
    if _pipeline_state["status"] == "running":
        raise HTTPException(status_code=409, detail="Pipeline already running")

    # Clear quality flags so they are freshly derived
    async with get_db() as db:
        await db.execute("""
            UPDATE media_files
            SET blur_score      = NULL,
                is_blurry       = NULL,
                long_exposure   = NULL,
                has_closed_eyes = NULL
        """)

    _pipeline_state.update({"status": "running", "folder": "[library reprocess]", "error": None})
    background_tasks.add_task(_run_reprocess, req.force_retag)
    return {"status": "started"}


@router.get("/status")
async def get_status():
    """Current pipeline status."""
    return _pipeline_state


async def _run_pipeline(folder: str) -> None:
    from backend.pipeline.ingest import run_ingest
    try:
        await run_ingest(folder)
        # Update scan_state with final file count and idle status
        async with get_db() as db:
            await db.execute("""
                UPDATE scan_state
                SET status       = 'idle',
                    last_scan_at = datetime('now'),
                    file_count   = (
                        SELECT COUNT(*) FROM media_files
                        WHERE file_path LIKE ? || '/%'
                          AND removed_from_app = 0
                    )
                WHERE folder_path = ?
            """, (folder, folder))
        _pipeline_state["status"] = "idle"
    except Exception as e:
        logger.exception("Pipeline error")
        async with get_db() as db:
            await db.execute(
                "UPDATE scan_state SET status='error' WHERE folder_path=?",
                (folder,)
            )
        _pipeline_state["status"] = "error"
        _pipeline_state["error"] = str(e)


async def _run_reprocess(force_retag: bool = False) -> None:
    from backend.pipeline.ingest import run_reprocess
    try:
        await run_reprocess(force_retag=force_retag)
        _pipeline_state["status"] = "idle"
    except Exception as e:
        logger.exception("Reprocess error")
        _pipeline_state["status"] = "error"
        _pipeline_state["error"] = str(e)


@router.post("/reprocess/{media_id}")
async def reprocess_photo(media_id: int, background_tasks: BackgroundTasks):
    """
    Re-detect faces in a single photo.

    Clears unowned face data for the photo, resets it to 'scanned', then
    runs the embed → cluster → auto-merge → name-restore phases.  Useful
    when a face was missed on the initial scan.  Named-person assignments
    for this photo are preserved.
    """
    if _pipeline_state["status"] == "running":
        raise HTTPException(status_code=409, detail="Pipeline already running")

    _pipeline_state.update({
        "status": "running",
        "folder": f"[reprocess photo {media_id}]",
        "error": None,
    })
    background_tasks.add_task(_run_single_reprocess, media_id)
    return {"status": "started", "media_id": media_id}


async def _run_single_reprocess(media_id: int) -> None:
    from backend.pipeline.ingest import run_single_reprocess
    try:
        await run_single_reprocess(media_id)
        _pipeline_state["status"] = "idle"
    except Exception as e:
        logger.exception("Single-photo reprocess error for media_id=%d", media_id)
        _pipeline_state["status"] = "error"
        _pipeline_state["error"] = str(e)


class BatchReprocessRequest(BaseModel):
    media_ids: list[int]


@router.post("/reprocess_batch")
async def reprocess_batch(req: BatchReprocessRequest, background_tasks: BackgroundTasks):
    """
    Re-detect faces in a batch of selected photos.

    Accepts a list of media IDs, clears unowned face data for each, resets
    them to 'scanned', then runs embed → cluster → auto-merge → name-restore
    across all of them in a single efficient pass.  Named-person assignments
    are preserved.
    """
    if not req.media_ids:
        raise HTTPException(status_code=400, detail="media_ids must not be empty")
    if _pipeline_state["status"] == "running":
        raise HTTPException(status_code=409, detail="Pipeline already running")

    label = f"[reprocess {len(req.media_ids)} photo{'s' if len(req.media_ids) != 1 else ''}]"
    _pipeline_state.update({"status": "running", "folder": label, "error": None})
    background_tasks.add_task(_run_batch_reprocess, list(req.media_ids))
    return {"status": "started", "count": len(req.media_ids)}


async def _run_batch_reprocess(media_ids: list[int]) -> None:
    from backend.pipeline.ingest import run_batch_reprocess
    try:
        await run_batch_reprocess(media_ids)
        _pipeline_state["status"] = "idle"
    except Exception as e:
        logger.exception("Batch reprocess error")
        _pipeline_state["status"] = "error"
        _pipeline_state["error"] = str(e)


@router.post("/migrate_model")
async def migrate_model(background_tasks: BackgroundTasks):
    """
    Re-embed all named faces with the currently configured model, recompute
    centroids, clear unnamed faces and re-cluster.

    Must be run after changing insightface_model in config and restarting
    the server.  Named person assignments are preserved.
    """
    if _pipeline_state["status"] == "running":
        raise HTTPException(status_code=409, detail="Pipeline already running")

    _pipeline_state.update({"status": "running", "folder": "[model migration]", "error": None})
    background_tasks.add_task(_run_model_migration)
    return {"status": "started"}


@router.post("/rebuild_clip_index")
async def rebuild_clip_index(background_tasks: BackgroundTasks):
    """Rebuild CLIP embeddings/index for all active photos only."""
    if _pipeline_state["status"] == "running":
        raise HTTPException(status_code=409, detail="Pipeline already running")

    _pipeline_state.update({"status": "running", "folder": "[clip index rebuild]", "error": None})
    background_tasks.add_task(_run_clip_index_rebuild)
    return {"status": "started"}


async def _run_model_migration() -> None:
    from backend.pipeline.ingest import run_model_migration
    try:
        await run_model_migration()
        _pipeline_state["status"] = "idle"
    except Exception as e:
        logger.exception("Model migration error")
        _pipeline_state["status"] = "error"
        _pipeline_state["error"] = str(e)


async def _run_clip_index_rebuild() -> None:
    from backend.pipeline.ingest import run_clip_index_rebuild
    try:
        await run_clip_index_rebuild()
        _pipeline_state["status"] = "idle"
    except Exception as e:
        logger.exception("CLIP index rebuild error")
        _pipeline_state["status"] = "error"
        _pipeline_state["error"] = str(e)
