"""
VIP Scanner — recursive folder walker with iCloud stub detection.

Key behaviours:
  - Yields only fully materialised RAW files (not iCloud stubs)
  - Respects supported_formats from config
  - Emits progress callbacks for WebSocket updates
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from backend.config import settings

logger = logging.getLogger(__name__)

# macOS extended attribute set on iCloud Drive files.
# When set to b'\x01', the file body is local. When absent or b'\x00', it's a stub.
_ICLOUD_ATTR = "com.apple.ubiquity.itemhascontents"


def _is_icloud_stub(path: Path) -> bool:
    """
    Return True if this file is an iCloud stub (not downloaded).

    iCloud stubs only exist inside the local user's home directory tree.
    Files on mounted volumes (/Volumes/…) — network shares (SMB/NFS/AFP)
    and external drives — are never iCloud placeholders, so skip both
    the xattr check and the size-based fallback entirely for those paths.
    Applying the size fallback on a NAS would incorrectly mark small-but-
    valid JPEGs as stubs and cause them to be silently skipped in Phase 2.
    """
    # Mounted volumes are never managed by iCloud.
    try:
        if str(path.resolve()).startswith("/Volumes/"):
            return False
    except OSError:
        pass

    try:
        import xattr  # type: ignore
        val = xattr.getxattr(str(path), _ICLOUD_ATTR)
        if val == b"\x00" or val == b"":
            return True
    except (OSError, KeyError):
        # xattr not present — fall back to size check
        pass
    except ImportError:
        # xattr package not installed — size check only
        pass

    # Size fallback: known RAW formats are always > 4KB
    try:
        return path.stat().st_size < settings.stub_max_size_bytes
    except OSError:
        return True  # can't stat → treat as unavailable


async def walk_folder(
    folder: Path,
    on_file: Optional[Callable[[Path, int, int], None]] = None,
) -> AsyncIterator[Path]:
    """
    Async generator — yields Path for each valid, fully materialised RAW file.

    Args:
        folder:   Root directory to scan recursively.
        on_file:  Optional callback(path, files_done, files_found) for progress.

    Yields:
        Path objects for each scannable file.
    """
    folder = folder.resolve()
    if not folder.is_dir():
        logger.error("Folder does not exist or is not a directory: %s", folder)
        return

    logger.info("Starting walk: %s", folder)
    found = 0
    skipped_format = 0
    skipped_stub = 0

    # os.walk is synchronous — run in thread pool to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    entries: list[Path] = await loop.run_in_executor(
        None, _collect_files, folder
    )

    total = len(entries)
    logger.info("Found %d candidate files in %s", total, folder)

    for i, path in enumerate(entries):
        suffix = path.suffix.lower()

        if suffix not in settings.supported_formats:
            skipped_format += 1
            continue

        if _is_icloud_stub(path):
            logger.debug("Skipping iCloud stub: %s", path)
            skipped_stub += 1
            continue

        found += 1
        if on_file:
            on_file(path, found, total)

        yield path

        # Yield control periodically so event loop can process WebSocket messages
        if found % 100 == 0:
            await asyncio.sleep(0)

    logger.info(
        "Walk complete: %d yielded, %d skipped (format), %d skipped (stub)",
        found,
        skipped_format,
        skipped_stub,
    )


def _collect_files(folder: Path) -> list[Path]:
    """Synchronous recursive file collection — called in executor."""
    result = []
    for root, _dirs, files in os.walk(folder, followlinks=False):
        for filename in files:
            result.append(Path(root) / filename)
    return result
