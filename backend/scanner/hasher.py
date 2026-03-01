"""
VIP Scanner — SHA-256 file hashing and idempotency checks.

Rules:
  - SHA-256 of file content is the true identity of a file (not its path)
  - If hash exists in DB → skip (already processed)
  - If path changed but hash exists → update path, do not reprocess
  - Large files: stream in 8MB chunks to avoid loading full CR3 into RAM
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB


def compute_hash(path: Path) -> str:
    """
    Compute SHA-256 of a file.  Streams in chunks — safe for 60MB CR3 files.
    Returns hex string.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


async def check_idempotency(
    db: aiosqlite.Connection,
    file_hash: str,
    file_path: str,
) -> tuple[bool, int | None]:
    """
    Check whether this file has already been processed.

    Returns:
        (skip, media_file_id)
        skip=True  → already processed, no further action needed
        skip=False → new file, needs ingest
        media_file_id → existing DB id if skip=True (for path update), else None
    """
    row = await (
        await db.execute(
            "SELECT id, file_path, needs_reprocess, removed_from_app FROM media_files WHERE file_hash = ?",
            (file_hash,),
        )
    ).fetchone()

    if row is None:
        # New file — not seen before
        return False, None

    existing_id, existing_path, needs_reprocess, removed = (
        row["id"], row["file_path"], row["needs_reprocess"], row["removed_from_app"]
    )

    if needs_reprocess or removed:
        # Re-evaluate: either explicitly requested or the file was soft-removed
        # and is being re-added by re-scanning the folder.
        if removed:
            logger.info("Re-adding previously removed file: %s (id=%d)", file_path, existing_id)
        else:
            logger.debug("Reprocess requested: %s (id=%d)", file_path, existing_id)
        return False, existing_id

    # Already processed — update path if it moved
    if existing_path != file_path:
        logger.info("File moved: %s → %s (id=%d)", existing_path, file_path, existing_id)
        await db.execute(
            "UPDATE media_files SET file_path = ?, last_seen_at = datetime('now') WHERE id = ?",
            (file_path, existing_id),
        )

    return True, existing_id
