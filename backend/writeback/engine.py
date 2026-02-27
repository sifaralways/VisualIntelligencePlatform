"""
VIP Writeback — dry-run / confirm / execute engine.

Flow:
  1. User names people in UI  →  DB updated, writeback_queue populated
  2. User opens /writeback page  →  GET /api/writeback/preview  (dry-run)
  3. User reviews changes  →  POST /api/writeback/confirm
  4. Engine writes via ExifTool, updates writeback_queue status
  5. User can purge backups via UI once satisfied
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.config import settings
from backend.database.db import get_db
from backend.writeback.exiftool import ExifToolWriter
from backend.writeback.fields import build_field_map, FaceRegion

logger = logging.getLogger(__name__)

_writer = ExifToolWriter()


async def preview_pending() -> list[dict]:
    """
    Return a dry-run preview of all pending writes.
    Nothing is written. Returns list of preview dicts.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT wq.id, wq.media_file_id, mf.file_path, mf.writeback_done
            FROM writeback_queue wq
            JOIN media_files mf ON mf.id = wq.media_file_id
            WHERE wq.status = 'pending'
        """)

    previews = []
    for row in rows:
        fields = await _build_fields_for_file(row["media_file_id"])
        previews.append({
            "queue_id": row["id"],
            "media_file_id": row["media_file_id"],
            "file_path": row["file_path"],
            "fields": fields,
        })
    return previews


async def execute_writes(queue_ids: list[int] | None = None) -> dict:
    """
    Execute ExifTool writes for confirmed queue items.

    Args:
        queue_ids: Specific queue IDs to write, or None for all pending.

    Returns:
        Summary dict with written/failed counts.
    """
    async with get_db() as db:
        if queue_ids:
            placeholders = ",".join("?" * len(queue_ids))
            rows = await db.execute_fetchall(
                f"SELECT wq.id, wq.media_file_id, mf.file_path, mf.writeback_done "
                f"FROM writeback_queue wq JOIN media_files mf ON mf.id=wq.media_file_id "
                f"WHERE wq.id IN ({placeholders}) AND wq.status='pending'",
                queue_ids,
            )
        else:
            rows = await db.execute_fetchall("""
                SELECT wq.id, wq.media_file_id, mf.file_path, mf.writeback_done
                FROM writeback_queue wq
                JOIN media_files mf ON mf.id = wq.media_file_id
                WHERE wq.status = 'pending'
            """)

    written = 0
    failed = 0

    for row in rows:
        queue_id = row["id"]
        media_id = row["media_file_id"]
        file_path = Path(row["file_path"])
        is_first_write = not bool(row["writeback_done"])

        fields = await _build_fields_for_file(media_id)
        if not fields:
            logger.info("No fields to write for media_id=%d, skipping", media_id)
            continue

        success, msg = _writer.write(
            file_path,
            fields,
            dry_run=False,
            is_first_write=is_first_write,
        )

        async with get_db() as db:
            if success:
                await db.execute("""
                    UPDATE writeback_queue
                    SET status='written', written_at=datetime('now')
                    WHERE id=?
                """, (queue_id,))
                await db.execute("""
                    UPDATE media_files
                    SET writeback_done=1, writeback_at=datetime('now')
                    WHERE id=?
                """, (media_id,))
                written += 1
            else:
                await db.execute("""
                    UPDATE writeback_queue
                    SET status='failed', error_msg=?
                    WHERE id=?
                """, (msg, queue_id))
                failed += 1

        logger.info("Write %s for %s: %s", "OK" if success else "FAILED", file_path.name, msg)

    return {"written": written, "failed": failed}


async def _build_fields_for_file(media_file_id: int) -> dict:
    """Assemble XMP field map for a given media file."""
    async with get_db() as db:
        # Get all named persons appearing in this file
        person_rows = await db.execute_fetchall("""
            SELECT DISTINCT p.name, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h
            FROM faces f
            JOIN persons p ON p.id = f.person_id
            WHERE f.media_file_id = ?
              AND p.name IS NOT NULL
              AND p.is_merged = 0
        """, (media_file_id,))

    if not person_rows:
        return {}

    names = [r["name"] for r in person_rows]
    regions = [
        FaceRegion(
            name=r["name"],
            x=r["bbox_x"] + r["bbox_w"] / 2,  # MWG uses centre point
            y=r["bbox_y"] + r["bbox_h"] / 2,
            w=r["bbox_w"],
            h=r["bbox_h"],
        )
        for r in person_rows
        if r["bbox_x"] is not None
    ]

    return build_field_map(person_names=names, face_regions=regions)
