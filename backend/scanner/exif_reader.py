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
from pathlib import Path
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)


class ExifToolReader:
    """
    Long-lived ExifTool process in stay_open mode.

    Usage:
        async with ExifToolReader() as reader:
            meta = await reader.read(path)
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    async def __aenter__(self) -> "ExifToolReader":
        loop = asyncio.get_event_loop()
        self._proc = await loop.run_in_executor(None, self._start)
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _start(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [
                "exiftool",
                "-stay_open", "True",
                "-@", "-",              # read args from stdin
                "-common_args",
                "-json",
                "-charset", "filename=UTF8",
                "-q",                   # quiet: suppress warnings to stdout
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.debug("ExifTool stay_open process started (pid=%d)", proc.pid)
        return proc

    async def read(self, path: Path) -> dict[str, Any]:
        """Read EXIF metadata for a single file. Returns normalised dict."""
        raw = await self._execute(str(path))
        if not raw:
            return {}
        return _normalise(raw[0])

    async def read_batch(self, paths: list[Path]) -> list[dict[str, Any]]:
        """Read EXIF for a batch of files. More efficient than calling read() N times."""
        if not paths:
            return []
        args = "\n".join(str(p) for p in paths)
        raw = await self._execute(args)
        return [_normalise(r) for r in raw]

    async def _execute(self, args_block: str) -> list[dict[str, Any]]:
        """
        Send a block of arguments to the stay_open process and read JSON response.
        ExifTool signals end of output with {ready} on its own line.
        """
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("ExifTool process is not running")

        command = f"{args_block}\n-execute\n".encode()
        loop = asyncio.get_event_loop()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._read_response, command),
                timeout=settings.exiftool_timeout_sec,
            )
            return json.loads(result) if result.strip() else []
        except asyncio.TimeoutError:
            logger.error("ExifTool timed out reading: %s", args_block[:100])
            return []
        except json.JSONDecodeError as e:
            logger.error("ExifTool JSON parse error: %s", e)
            return []

    def _read_response(self, command: bytes) -> str:
        """Synchronous — runs in executor thread."""
        assert self._proc and self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(command)
        self._proc.stdin.flush()

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
