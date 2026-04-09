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

# Formats that are themselves the full image (no embedded RAW preview needed).
# Pillow opens these directly and saves a normalised JPEG to the preview dir.
_DIRECT_IMAGE_SUFFIXES: frozenset[str] = frozenset({
    ".jpg", ".jpeg",        # JPEG — Pillow
    ".avif",               # AVIF — sips
    ".heic", ".heif",      # HEIC/HEIF — sips
    ".png",                # PNG — Pillow
    ".webp",               # WebP — Pillow
    ".tiff", ".tif",       # TIFF — Pillow
    ".psd",                # Photoshop — sips
})

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
    Produce a normalised JPEG preview suitable for ML inference.

    For RAW files (CR3, ARW, NEF, …): extracts the largest embedded JPEG
    using ExifTool (fast, camera-agnostic, no full RAW decode).

    For direct image formats (JPEG, AVIF): opens with Pillow, applies EXIF
    orientation, converts to RGB, and saves as JPEG to the preview dir.

    Returns:
        Path to the JPEG in preview_dir, or None on failure.
    """
    if raw_path.suffix.lower() in _DIRECT_IMAGE_SUFFIXES:
        return await _preview_from_direct_image(raw_path)

    out_path = settings.preview_dir / f"{raw_path.stem}_{_hash_path(raw_path)}.jpg"

    if out_path.exists():
        logger.debug("Preview already exists: %s", out_path)
        return out_path

    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(
        None, _extract_sync, raw_path, out_path
    )
    return out_path if success else None


async def _preview_from_direct_image(image_path: Path) -> Optional[Path]:
    """
    Convert a JPEG or AVIF file to a normalised JPEG in the preview dir.

    Orientation is corrected via EXIF transpose so that all downstream
    consumers receive geometrically upright pixels without any extra handling.
    Alpha channels (possible in AVIF) are dropped via conversion to RGB.
    """
    out_path = settings.preview_dir / f"{image_path.stem}_{_hash_path(image_path)}.jpg"
    if out_path.exists():
        logger.debug("Preview already exists: %s", out_path)
        return out_path

    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(
        None, _direct_image_to_jpeg, image_path, out_path
    )
    return out_path if success else None


def _direct_image_to_jpeg(src: Path, dst: Path) -> bool:
    """
    Convert a direct image (JPEG or AVIF) to a normalised JPEG in the preview
    dir.  Runs in an executor thread.

    AVIF: macOS `sips` CLI is used because standard Pillow pip wheels have no
    AVIF codec.  `sips` delegates to Apple's native image pipeline — no extra
    dependencies, works on every Apple Silicon Mac.

    JPEG: Pillow handles conversion; EXIF orientation is applied so all
    downstream models receive geometrically upright pixels.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    # HEIC, HEIF, AVIF and PSD are decoded via macOS sips — Pillow has no
    # native codec for these formats in standard pip wheels.
    _SIPS_SUFFIXES = frozenset({".avif", ".heic", ".heif", ".psd"})
    if src.suffix.lower() in _SIPS_SUFFIXES:
        return _sips_to_jpeg(src, dst)

    # PNG, WebP, TIFF, JPEG — Pillow handles all of these natively.
    try:
        from PIL import Image, ImageOps
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(dst, "JPEG", quality=92, optimize=True)
        logger.debug("Direct image preview saved: %s → %s", src.name, dst.name)
        return True
    except Exception as e:
        logger.error("Failed to convert %s to JPEG preview: %s", src.name, e)
        return False


def _sips_to_jpeg(src: Path, dst: Path) -> bool:
    """
    Convert an image to JPEG using macOS `sips`.

    Used for formats Pillow cannot decode natively in standard pip wheels:
    AVIF, HEIC, HEIF, PSD.  `sips` is bundled with every macOS installation
    and delegates to Apple's native ImageIO framework — no extra dependencies.
    """
    try:
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "92",
             str(src), "--out", str(dst)],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.error(
                "sips failed for %s (exit %d): %s",
                src.name, result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )
            return False
        logger.debug("%s → JPEG via sips: %s → %s", src.suffix.upper(), src.name, dst.name)
        return True
    except subprocess.TimeoutExpired:
        logger.error("sips timed out converting: %s", src.name)
        return False
    except FileNotFoundError:
        logger.error("`sips` not found — should be present on every macOS installation")
        return False
    except Exception as e:
        logger.error("sips conversion error for %s: %s", src.name, e)
        return False


def _extract_sync(raw_path: Path, out_path: Path) -> bool:
    """
    Synchronous ExifTool call — runs in executor thread.

    ExifTool flags used:
      -b             output binary data
      -JpgFromRaw    full sensor resolution JPEG embedded in the RAW (first choice)
      -fast          skip trailer scan only; required to reach JpgFromRaw in CR3
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

        # Tag priority — highest resolution first:
        #   JpgFromRaw        Canon CR3/Nikon: full sensor JPEG (6000×4000 etc.)
        #                     Same pixels the camera would write for a JPEG shot.
        #   LargePreviewImage Some cameras embed a separate high-res preview.
        #   PreviewImage      Fallback: usually 1620×1080 — good enough for
        #                     thumbnails but too small for group-shot face detection.
        # Never use -PreviewImage as the first choice: Canon CR3 always has one
        # at ~1620×1080 so we would never reach the 6000×4000 JpgFromRaw.
        for tag in ("-JpgFromRaw", "-LargePreviewImage", "-PreviewImage"):
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
            # No embedded JPEG in this RAW file (common with Google Photos DNG
            # exports and some other camera-variant DNGs).  Fall back to sips
            # which can decode RAW pixel data via Apple's native ImageIO.
            logger.warning(
                "No embedded JPEG found in %s — falling back to sips RAW decode",
                raw_path.name,
            )
            sips_ok = _sips_to_jpeg(raw_path, out_path)
            if not sips_ok:
                logger.warning("sips fallback also failed for: %s", raw_path)
                return False
            # sips does not preserve RAW orientation — apply it now.
            _correct_preview_orientation(out_path, orientation)
            return True

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
