"""
VIP Scanner — ExifTool EXIF/metadata extraction.

One subprocess.run() call per file with a hard OS-level timeout.

Previous approach (stay_open batch mode) caused ExifTool to hang indefinitely
on specific CR3 files, requiring a 120-second wait before the pipeline could
move on. Per-file subprocess is ~25 ms slower per file but never hangs,
always terminates cleanly, and is far simpler to reason about.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Hard OS-level timeout per file. ExifTool on a local 60 MB CR3 should finish
# in well under 5 seconds. 30 s gives plenty of headroom.
_PER_FILE_TIMEOUT = 30


def materialise_file(path: Path) -> bool:
    """
    Check that the file is accessible. For iCloud stubs, the stat() call
    itself triggers macOS to begin downloading the file synchronously.

    Returns True if the file exists and is readable.
    """
    try:
        return path.is_file()
    except OSError as e:
        logger.warning("Cannot access %s: %s", path.name, e)
        return False


def _run_exiftool(path: Path) -> dict[str, Any] | None:
    """
    Run ExifTool on a single file. Blocking — call via run_in_executor.

    Returns the first element of the JSON array, or None on any failure.
    """
    try:
        result = subprocess.run(
            [
                "exiftool",
                "-json",
                "-fast2",
                "-charset", "filename=UTF8",
                "-q",
                str(path),
            ],
            capture_output=True,
            timeout=_PER_FILE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "ExifTool timed out (%ds) on %s — skipping", _PER_FILE_TIMEOUT, path.name
        )
        return None
    except FileNotFoundError:
        logger.error("exiftool not found — install with: brew install exiftool")
        return None
    except Exception as e:
        logger.error("ExifTool error on %s: %s", path.name, e)
        return None

    if result.returncode != 0:
        logger.debug("ExifTool returned %d for %s", result.returncode, path.name)
        return None

    try:
        parsed = json.loads(result.stdout)
        return parsed[0] if parsed else None
    except json.JSONDecodeError as e:
        logger.error("ExifTool JSON parse error for %s: %s", path.name, e)
        return None


class ExifToolReader:
    """
    Async wrapper around per-file ExifTool subprocess calls.

    Keeps the same context-manager interface as the old stay_open version
    so call sites in the pipeline do not need to change.
    """

    async def __aenter__(self) -> "ExifToolReader":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass  # nothing to clean up

    async def read(self, path: Path) -> dict[str, Any]:
        """Read EXIF metadata for a single file. Returns normalised dict."""
        if not materialise_file(path):
            logger.warning("Cannot access %s — skipping", path.name)
            return {}

        logger.debug("ExifTool reading %s", path.name)
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, _run_exiftool, path)

        if raw is None:
            return {}

        normalised = _normalise(raw)
        logger.debug(
            "ExifTool done  %s  camera=%s  date=%s",
            path.name,
            normalised.get("camera_model"),
            normalised.get("date_taken"),
        )
        return normalised

    async def read_batch(self, paths: list[Path]) -> list[dict[str, Any]]:
        """Read EXIF for a list of files sequentially."""
        results = []
        for path in paths:
            results.append(await self.read(path))
        return results


def _parse_float(value: Any) -> float | None:
    """
    Safely coerce a value from ExifTool to a Python float.
    Returns None if the value cannot be parsed.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_exposure_time(value: Any) -> float | None:
    """
    Convert ExifTool's ExposureTime to a float (seconds).

    ExifTool JSON mode usually returns a float, but occasionally emits a
    fraction string such as "1/500" or "1/8000".  We evaluate it safely
    by splitting on '/' rather than using eval().
    """
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        pass
    try:
        s = str(value).strip()
        if '/' in s:
            num, den = s.split('/', 1)
            return float(num) / float(den)
    except Exception:
        pass
    return None


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Map ExifTool's verbose field names to our internal schema field names.
    Returns only the fields we care about.
    """
    def _get(*keys: str) -> Any:
        for k in keys:
            v = raw.get(k)
            if v is not None:
                return v
        return None

    # ---------------------------------------------------------------------------
    # Pre-existing rich XMP/IPTC fields — captured as a snapshot so we can
    # show them in the Analysis UI as "VIP History" or "External History".
    # ---------------------------------------------------------------------------
    # XMP:Identifier — if present, VIP previously wrote this file (we always
    # write our vip_id UUID here).  Used to distinguish VIP History from
    # External History in the analysis panel.
    xmp_identifier = _get("Identifier", "XMP:Identifier")

    # XMP:PersonInImage — named people (VIP writes here; Apple Photos / LR too)
    _raw_persons = _get("PersonInImage", "XMP:PersonInImage")
    xmp_persons: list[str] | None = None
    if _raw_persons is not None:
        xmp_persons = [_raw_persons] if isinstance(_raw_persons, str) else list(_raw_persons)

    # XMP:Subject / IPTC:Keywords — flat keyword list
    _raw_kw = _get("Subject", "XMP:Subject", "Keywords", "IPTC:Keywords")
    xmp_keywords: list[str] | None = None
    if _raw_kw is not None:
        xmp_keywords = [_raw_kw] if isinstance(_raw_kw, str) else list(_raw_kw)

    # XMP:Location — free-text location (IPTC Core)
    xmp_location = _get("Location", "XMP:Location")

    # XMP-mwg-rs:RegionInfo — MWG face region structs (Name + bbox per face)
    # ExifTool returns this as a dict: {"AppliedToDimensions": {...}, "RegionList": [...]}
    xmp_region_info = raw.get("RegionInfo") or raw.get("XMP-mwg-rs:RegionInfo")

    return {
        "file_path":       raw.get("SourceFile"),
        "file_format":     _get("FileType"),
        "camera_make":     _get("Make"),
        "camera_model":    _get("Model"),
        "date_taken":      _get("DateTimeOriginal", "CreateDate", "ModifyDate"),
        "gps_lat":         _parse_float(_get("GPSLatitude")),
        "gps_lon":         _parse_float(_get("GPSLongitude")),
        "width":           _get("ImageWidth", "ExifImageWidth"),
        "height":          _get("ImageHeight", "ExifImageHeight"),
        # Shutter speed in seconds (e.g. 0.005 for 1/200 s, 2.0 for 2 s).
        # ExifTool may return a fraction string ("1/500") — parse to float.
        "exposure_time_s": _parse_exposure_time(_get("ExposureTime")),
        # ── Pre-existing rich metadata (for VIP/External History display) ──
        "xmp_identifier":  xmp_identifier,   # our UUID if previously VIP-processed
        "xmp_persons":     xmp_persons,       # named persons already in file
        "xmp_keywords":    xmp_keywords,      # flat keyword list already in file
        "xmp_location":    xmp_location,      # free-text location already in file
        "xmp_region_info": xmp_region_info,   # MWG face regions already in file
    }
