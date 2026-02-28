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

Orientation handling:
  Canon CR3 (and many other cameras) embed the preview JPEG with Orientation=1
  (normal) regardless of how the camera was held, even though the RAW file's
  own Orientation tag correctly describes the capture rotation.  Relying on the
  *preview's* EXIF orientation tag is therefore wrong — we must read it from the
  parent RAW and apply the rotation to the pixel data ourselves.  This makes the
  saved preview physically upright so that every downstream consumer (InsightFace,
  YOLO, Places365, CLIP, BioCLIP, and the UI thumbnail generator) receives correct
  geometry without any changes to their own code.
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

# Maps numeric EXIF Orientation tag value → PIL Transpose method name.
# Identical to the mapping used internally by PIL.ImageOps.exif_transpose,
# except we read the tag from the RAW file, not from the (often wrong) preview.
_EXIF_ORIENT_TO_PIL_TRANSPOSE = {
    2: "FLIP_LEFT_RIGHT",   # mirrored horizontally
    3: "ROTATE_180",        # upside-down
    4: "FLIP_TOP_BOTTOM",   # mirrored vertically
    5: "TRANSPOSE",         # 90°CW + mirror
    6: "ROTATE_270",        # 90°CW (camera held right-side-right, sensor landscape)
    7: "TRANSVERSE",        # 90°CCW + mirror
    8: "ROTATE_90",         # 90°CCW / 270°CW (camera held left-side-right)
}


def _read_raw_orientation(raw_path: Path) -> int:
    """
    Read the EXIF Orientation tag (numeric value) from the RAW file.

    Returns 1 (upright, no-op) on any error.
    Uses the '#' notation so ExifTool returns the integer, not a string.
    """
    try:
        r = subprocess.run(
            ["exiftool", "-s3", "-n", "-Orientation", str(raw_path)],
            capture_output=True, text=True, timeout=15,
        )
        val = r.stdout.strip()
        return int(val) if val.isdigit() else 1
    except Exception:
        return 1


def _correct_preview_orientation(out_path: Path, orientation: int) -> None:
    """
    Apply a rotation/flip to the JPEG at *out_path* to bake in the correct
    geometry described by *orientation* (EXIF tag 274 value read from RAW).

    The saved file will have tag=1 (no further rotation needed) so that any
    tool that reads the preview in isolation gets the right result.
    """
    if orientation == 1:
        return   # already upright — nothing to do

    method_name = _EXIF_ORIENT_TO_PIL_TRANSPOSE.get(orientation)
    if method_name is None:
        logger.warning("Unknown EXIF orientation value %d — skipping correction", orientation)
        return

    try:
        from PIL import Image
        method = getattr(Image.Transpose, method_name)
        with Image.open(out_path) as img:
            corrected = img.transpose(method)
            # Strip the orientation tag — pixels are now geometrically correct
            exif = img.getexif()
            exif[274] = 1
            corrected.save(out_path, "JPEG", quality=92, optimize=True, exif=exif.tobytes())
        logger.debug(
            "Orientation corrected (tag %d → %s): %s",
            orientation, method_name, out_path.name,
        )
    except Exception as exc:
        logger.warning("Orientation correction failed for %s: %s", out_path.name, exc)


async def extract_preview(raw_path: Path) -> Optional[Path]:
    """
    Extract the largest embedded JPEG preview from a RAW file.

    Returns:
        Path to the extracted JPEG in preview_dir, or None on failure.
    """
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
    """
    try:
        _timeout = 60
        if not materialise_file(raw_path):
            return False

        # Read orientation from the RAW before touching the preview.
        # Canon CR3 (and many other cameras) embed the preview with Orientation=1
        # even when the RAW itself is tagged as portrait — we must apply the
        # parent RAW's rotation to the extracted JPEG ourselves.
        orientation = _read_raw_orientation(raw_path)

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

        # Bake the RAW's orientation into the preview pixel data.
        _correct_preview_orientation(out_path, orientation)

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
