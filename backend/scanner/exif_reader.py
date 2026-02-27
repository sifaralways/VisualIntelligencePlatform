"""
VIP Scanner — ExifTool EXIF/metadata extraction.

Uses ExifTool in "stay_open" batch mode for performance.
A single ExifTool process is reused for the life of the pipeline run
rather than spawning one process per file (~25ms startup cost each).

Output is JSON from ExifTool. Fields are normalised to our schema.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

# Per-file timeout. Must cover iCloud on-demand download of a 40 MB CR3
# on a slow connection before ExifTool can read it.
_PER_FILE_TIMEOUT = 120


def materialise_file(path: Path) -> bool:
    """
    Force iCloud to fully download a file before we read it with ExifTool.

    Strategy:
      1. `brctl download <path>` — the macOS iCloud control tool that blocks
         until the file is 100% local. This is the correct API.
      2. Fallback: read the whole file into /dev/null, which forces a full
         APFS file-provider materialisation before returning.

    Returns True if the file is readable and local, False on any error.
    """
    import subprocess

    # Fast path: brctl is available on all macOS versions with iCloud Drive
    try:
        result = subprocess.run(
            ["brctl", "download", str(path)],
            timeout=300,   # 5 min max — enough for a 60 MB CR3 on slow iCloud
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        # brctl may return non-zero even on success for already-local files
        # fall through to the read fallback
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug("brctl unavailable or timed out (%s) — falling back to full read", e)

    # Fallback: read every byte to force full materialisation
    try:
        with open(path, 'rb') as f:
            while f.read(8 * 1024 * 1024):  # 8 MB chunks
                pass
        return True
    except OSError as e:
        logger.warning("Cannot materialise %s: %s", path.name, e)
        return False


class ExifToolReader:
    """
    Long-lived ExifTool process in stay_open mode.

    Usage:
        async with ExifToolReader() as reader:
            meta = await reader.read(path)
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = asyncio.Lock()   # serialise calls; stdout is a sequential stream

    async def __aenter__(self) -> "ExifToolReader":
        self._start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _start(self) -> None:
        self._proc = subprocess.Popen(
            [
                "exiftool",
                "-stay_open", "True",
                "-@", "-",              # read args from stdin
                "-common_args",
                "-json",
                "-fast2",               # skip scanning past end-of-file (faster on CR3)
                "-charset", "filename=UTF8",
                "-q",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        logger.debug("ExifTool stay_open process started (pid=%d)", self._proc.pid)

    def _restart(self) -> None:
        """Kill the current (stuck) process and start a fresh one."""
        if self._proc:
            try:
                self._proc.kill()
                self._proc.wait(timeout=3)
            except Exception:
                pass
        self._proc = None
        self._start()
        logger.warning("ExifTool process restarted after timeout")

    async def read(self, path: Path) -> dict[str, Any]:
        """Read EXIF metadata for a single file. Returns normalised dict."""
        # Materialise from iCloud before handing to ExifTool.
        # run_in_executor so the blocking download doesn't stall the event loop.
        logger.debug("Materialising %s …", path.name)
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, materialise_file, path)
        if not ok:
            logger.warning("Could not materialise %s — skipping", path.name)
            return {}
        logger.debug("Materialised %s  ✓", path.name)
        async with self._lock:
            raw = await self._execute_one(path)
        if not raw:
            return {}
        return _normalise(raw)

    async def read_batch(self, paths: list[Path]) -> list[dict[str, Any]]:
        """
        Read EXIF for a list of files.

        We call read() per file (serialised via the lock) rather than sending
        all paths in a single -execute block. This prevents a single large-batch
        timeout from wedging the entire process and makes per-file error recovery
        straightforward.
        """
        results = []
        for path in paths:
            results.append(await self.read(path))
        return results

    async def _execute_one(self, path: Path) -> dict[str, Any] | None:
        """
        Send one file to the stay_open process and read the JSON response.
        On timeout, kill + restart the process so the next call is clean.
        Caller must hold self._lock.
        """
        if self._proc is None or self._proc.poll() is not None:
            self._start()

        command = f"{path}\n-execute\n".encode()
        loop = asyncio.get_event_loop()

        # We read stdout in a daemon thread. If it times out we kill the process,
        # which unblocks the thread (EOF on stdout), keeping the executor clean.
        future = loop.run_in_executor(None, self._read_response, command)
        try:
            result = await asyncio.wait_for(future, timeout=_PER_FILE_TIMEOUT)
            parsed = json.loads(result) if result.strip() else []
            return parsed[0] if parsed else None
        except asyncio.TimeoutError:
            logger.warning("ExifTool timed out on %s — restarting process", path.name)
            self._restart()
            return None
        except json.JSONDecodeError as e:
            logger.error("ExifTool JSON parse error for %s: %s", path.name, e)
            return None

    def _read_response(self, command: bytes) -> str:
        """Synchronous — runs in executor thread. Unblocks when process is killed."""
        assert self._proc and self._proc.stdin and self._proc.stdout
        try:
            self._proc.stdin.write(command)
            self._proc.stdin.flush()
        except BrokenPipeError:
            return ""

        output_lines = []
        for line in self._proc.stdout:
            decoded = line.decode("utf-8", errors="replace")
            if decoded.strip() == "{ready}":
                break
            output_lines.append(decoded)
        return "".join(output_lines)

    async def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write(b"-stay_open\nFalse\n")  # type: ignore
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            logger.debug("ExifTool process closed")
        self._proc = None


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

    return {
        "file_path":     raw.get("SourceFile"),
        "file_format":   _get("FileType"),
        "camera_make":   _get("Make"),
        "camera_model":  _get("Model"),
        "date_taken":    _get("DateTimeOriginal", "CreateDate", "ModifyDate"),
        "gps_lat":       _get("GPSLatitude"),
        "gps_lon":       _get("GPSLongitude"),
        "width":         _get("ImageWidth", "ExifImageWidth"),
        "height":        _get("ImageHeight", "ExifImageHeight"),
    }
