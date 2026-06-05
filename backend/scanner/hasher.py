"""
VIP Scanner — SHA-256 hashing and idempotency checks.

Rules:
    - Prefer stable in-file identifier (XMP:Identifier) when available.
    - Fall back to SHA-256 content hash for non-identified files.
    - If path changed, update path without creating a new DB row.
    - Large files: stream in 8MB chunks to avoid loading full CR3 into RAM.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
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
    stable_identifier: str | None = None,
) -> tuple[bool, int | None]:
    """
    Check whether this file has already been processed.

    Returns:
        (skip, media_file_id)
        skip=True  → already processed, no further action needed
        skip=False → new file, needs ingest
        media_file_id → existing DB id if skip=True (for path update), else None
    """
    row = None

    # 1) Strongest match: stable identifier written by VIP into XMP:Identifier.
    if stable_identifier:
        row = await (
            await db.execute(
                """
                SELECT id, file_path, file_hash, needs_reprocess, removed_from_app
                FROM media_files
                WHERE asset_id = ? OR vip_id = ?
                ORDER BY CASE WHEN asset_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (stable_identifier, stable_identifier, stable_identifier),
            )
        ).fetchone()

    # 2) Fallback: content hash.
    if row is None:
        row = await (
            await db.execute(
                "SELECT id, file_path, file_hash, needs_reprocess, removed_from_app FROM media_files WHERE file_hash = ?",
                (file_hash,),
            )
        ).fetchone()

    # 3) Path fallback: protects against duplicate-row creation when metadata
    # writes changed file bytes but the file has no XMP identifier yet.
    if row is None:
        row = await (
            await db.execute(
                """
                SELECT id, file_path, file_hash, needs_reprocess, removed_from_app
                FROM media_files
                WHERE file_path = ?
                ORDER BY removed_from_app ASC
                LIMIT 1
                """,
                (file_path,),
            )
        ).fetchone()

    if row is None:
        # New file — not seen before
        return False, None

    existing_id, existing_path, existing_hash, needs_reprocess, removed = (
        row["id"], row["file_path"], row["file_hash"], row["needs_reprocess"], row["removed_from_app"]
    )

    if needs_reprocess or removed:
        # Re-evaluate: either explicitly requested or the file was soft-removed
        # and is being re-added by re-scanning the folder.
        if removed:
            logger.info("Re-adding previously removed file: %s (id=%d)", file_path, existing_id)
        else:
            logger.debug("Reprocess requested: %s (id=%d)", file_path, existing_id)
        return False, existing_id

    # Already processed — update path/hash if needed.
    # Normalise both sides to NFC so that a path written on HFS+ (NFD) is not
    # mistakenly reported as moved when the same file is re-scanned over SMB (NFC).
    path_changed = unicodedata.normalize("NFC", existing_path) != unicodedata.normalize("NFC", file_path)
    hash_changed = existing_hash != file_hash

    if path_changed or hash_changed:
        if path_changed:
            logger.info("File moved: %s → %s (id=%d)", existing_path, file_path, existing_id)
        if hash_changed:
            logger.info("File content hash changed for existing asset id=%d", existing_id)
        try:
            await db.execute(
                "UPDATE media_files SET file_path = ?, file_hash = ?, last_seen_at = datetime('now') WHERE id = ?",
                (file_path, file_hash, existing_id),
            )
        except aiosqlite.IntegrityError:
            # Rare case: hash already claimed by another row (historical drift).
            # Prefer the canonical row that already owns the hash.
            clash = await (
                await db.execute(
                    "SELECT id, file_path FROM media_files WHERE file_hash = ? LIMIT 1",
                    (file_hash,),
                )
            ).fetchone()
            if clash and clash["id"] != existing_id:
                logger.warning(
                    "Hash collision while updating id=%d; using existing hash owner id=%d",
                    existing_id,
                    clash["id"],
                )
                clash_path = clash["file_path"]
                if unicodedata.normalize("NFC", clash_path) != unicodedata.normalize("NFC", file_path):
                    await db.execute(
                        "UPDATE media_files SET file_path = ?, last_seen_at = datetime('now') WHERE id = ?",
                        (file_path, clash["id"]),
                    )
                return True, int(clash["id"])
            raise

    return True, existing_id
