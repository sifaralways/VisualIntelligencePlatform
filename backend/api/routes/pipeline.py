"""
VIP API — Pipeline routes.
"""

from __future__ import annotations

import asyncio
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
