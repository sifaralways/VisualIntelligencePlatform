"""
VIP Scanner — embedded JPEG preview extraction from RAW files.

Extracts the full-resolution embedded JPEG preview that camera manufacturers
bake into every RAW file (CR3, ARW, NEF, DNG, etc.).

Why not decode the RAW?
  - A 60MB CR3 contains a ~8MP JPEG preview — sufficient for face detection
  - ExifTool extracts it in ~50ms vs ~3s for full RAW decode
  - No LibRaw/rawpy needed for the ML pipeline (only for UI display)
  - Works identically across Canon, Sony, Nikon, Fujifilm, etc.

The extracted JPEG is written to settings.preview_dir as a temp file.
After face embedding is complete, previews are deleted to reclaim disk space.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

from backend.config import settings
from backend.scanner.exif_reader import materialise_file

logger = logging.getLogger(__name__)


async def extract_preview(raw_path: Path) -> Optional[Path]:
    """
    Extract the largest embedded JPEG preview from a RAW file.

    Returns:
        Path to the extracted JPEG in preview_dir, or None on failure.
    """
    # Output filename: preserve stem, change suffix to .jpg
    out_path = settings.preview_dir / f"{raw_path.stem}_{_hash_path(raw_path)}.jpg"

    if out_path.exists():
        logger.debug("Preview already exists: %s", out_path)
        return out_path

    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(
        None, _extract_sync, raw_path, out_path
    )
    return out_path if success else None


def _extract_sync(raw_path: Path, out_path: Path) -> bool:
    """
    Synchronous ExifTool call — runs in executor thread.

    ExifTool flags used:
      -b           output binary data
      -PreviewImage  the embedded full-size JPEG preview
      -w! <ext>    write to file with given extension, overwrite if exists

    We redirect stdout to the output file directly.
    """
    try:
        _timeout = 60
        if not materialise_file(raw_path):
            return False

        # Tag priority: LargePreviewImage (Canon CR3 full-size) → PreviewImage → JpgFromRaw
        # Do NOT use -fast2; it stops before the large embedded JPEG in CR3 files.
        # -fast (no number) skips the trailer scan only — safe and still quick.
        for tag in ("-LargePreviewImage", "-PreviewImage", "-JpgFromRaw"):
            result = subprocess.run(
                [
                    "exiftool",
                    "-fast",
                    "-b",
                    tag,
                    "-charset", "filename=UTF8",
                    str(raw_path),
                ],
                capture_output=True,
                timeout=_timeout,
            )
            if result.stdout:
                logger.debug(
                    "Extracted preview via %s: %s → %s (%d bytes)",
                    tag, raw_path.name, out_path.name, len(result.stdout)
                )
                break

        if not result.stdout:
            logger.warning("No embedded JPEG found in: %s", raw_path)
            return False

        out_path.write_bytes(result.stdout)
        return True

    except subprocess.TimeoutExpired:
        logger.error("ExifTool timed out extracting preview: %s", raw_path)
        return False
    except Exception as e:
        logger.error("Preview extraction error for %s: %s", raw_path, e)
        return False


def _hash_path(path: Path) -> str:
    """Short stable suffix to avoid filename collisions across folders."""
    import hashlib
    return hashlib.md5(str(path).encode()).hexdigest()[:8]


async def delete_preview(preview_path: Path) -> None:
    """Remove a temp preview JPEG after embedding is complete."""
    try:
        preview_path.unlink(missing_ok=True)
        logger.debug("Deleted preview: %s", preview_path)
    except Exception as e:
        logger.warning("Could not delete preview %s: %s", preview_path, e)
