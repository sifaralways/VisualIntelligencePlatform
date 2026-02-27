"""VIP API — Writeback routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from backend.writeback.engine import preview_pending, execute_writes

router = APIRouter()


class ConfirmRequest(BaseModel):
    queue_ids: Optional[list[int]] = None   # None = confirm all pending


@router.get("/preview")
async def get_preview():
    """
    Dry-run: return exactly what ExifTool would write for all pending items.
    Nothing is changed. Shows file paths and fields to be written.
    Use this to review before confirming.
    """
    previews = await preview_pending()
    return {
        "count": len(previews),
        "items": previews,
        "warning": (
            "Files must be locally present (not iCloud stubs) when you confirm. "
            "ExifTool will create _original backups on first write."
        ),
    }


@router.post("/confirm")
async def confirm_writes(req: ConfirmRequest):
    """
    Execute pending writes. Files must be on local disk.
    Provide queue_ids to write a subset, or omit to write all pending.
    """
    result = await execute_writes(req.queue_ids)
    return result


@router.get("/status")
async def get_writeback_status():
    """Summary of writeback queue by status."""
    from backend.database.db import get_db
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT status, COUNT(*) as count
            FROM writeback_queue
            GROUP BY status
        """)
    return {r["status"]: r["count"] for r in rows}
