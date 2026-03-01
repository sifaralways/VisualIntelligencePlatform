"""
VIP API — Pipeline routes.
"""

from __future__ import annotations

import logging
import os
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

    _pipeline_state.update({"status": "running", "folder": str(folder), "error": None})
    background_tasks.add_task(_run_pipeline, str(folder))

    return {"status": "started", "folder": str(folder)}


@router.post("/rescan")
async def rescan_library(background_tasks: BackgroundTasks):
    """Force a complete rescan + reprocess of every file already in the library.

    Marks all media_files as needs_reprocess, clears derived quality signals,
    then runs the full pipeline.  Uses the last-known scan folder; falls back
    to the common directory ancestor of files stored in the database.
    """
    if _pipeline_state["status"] == "running":
        raise HTTPException(status_code=409, detail="Pipeline already running")

    # Prefer the in-memory last folder; fall back to DB heuristic
    folder_str: str | None = _pipeline_state.get("folder")
    if not folder_str:
        async with get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT file_path FROM media_files LIMIT 500"
            )
        if not rows:
            raise HTTPException(
                status_code=400,
                detail="No files in library yet. Run an initial scan first.",
            )
        # Find common directory ancestor of all known paths
        paths = [Path(r["file_path"]).parent for r in rows]
        common = paths[0]
        for p in paths[1:]:
            try:
                common = Path(os.path.commonpath([str(common), str(p)]))
            except ValueError:
                break
        folder_str = str(common)

    folder = Path(folder_str)
    if not folder.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Last scan folder no longer accessible: {folder_str}",
        )

    # Mark every existing file for reprocessing; clear derived quality fields
    async with get_db() as db:
        await db.execute("""
            UPDATE media_files
            SET needs_reprocess = 1,
                blur_score      = NULL,
                is_blurry       = NULL,
                long_exposure   = NULL,
                has_closed_eyes = NULL
        """)

    _pipeline_state.update({"status": "running", "folder": folder_str, "error": None})
    background_tasks.add_task(_run_pipeline, folder_str)
    return {"status": "started", "folder": folder_str}


@router.get("/status")
async def get_status():
    """Current pipeline status."""
    return _pipeline_state


async def _run_pipeline(folder: str) -> None:
    from backend.pipeline.ingest import run_ingest
    try:
        await run_ingest(folder)
        _pipeline_state["status"] = "idle"
    except Exception as e:
        logger.exception("Pipeline error")
        _pipeline_state["status"] = "error"
        _pipeline_state["error"] = str(e)
