"""
VIP Writeback — ExifTool subprocess wrapper.

Two write modes:
  - Single subprocess (default): one `exiftool` Perl process per file.
  - Persistent stay_open process: ExifTool runs once for the whole batch,
    accepting per-file commands via stdin and returning {ready} on stdout.
    Eliminates Perl interpreter startup (~200 ms) for every file write.
    Use ExifToolWriter.open() / .close() (or as a context manager) to
    activate persistent mode; write() auto-dispatches to it.

Principle: ExifTool writes atomically.
  1. ExifTool writes to a temp file first.
  2. If successful, renames temp → original.
  3. A crash mid-write leaves the original untouched.
  4. On first write: a _original backup is created automatically.
  5. On subsequent rewrites: -overwrite_original skips re-backup.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)


class ExifToolWriter:
    """Writes XMP metadata into RAW files via ExifTool CLI."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._stderr_buf: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Persistent process lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Start ExifTool in persistent -stay_open mode.

        All subsequent write() calls will use this process rather than
        spawning a new Perl interpreter per file.  Call close() when done,
        or use the instance as a context manager.
        """
        if self._proc is not None:
            return
        self._stderr_buf = []
        self._proc = subprocess.Popen(
            ["exiftool", "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Drain stderr in a daemon thread so it never blocks stdout reads.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="exiftool-stderr"
        )
        self._stderr_thread.start()
        logger.info("ExifTool persistent process started (PID=%d)", self._proc.pid)

    def close(self) -> None:
        """Gracefully terminate the persistent ExifTool process."""
        if self._proc is None:
            return
        try:
            self._proc.stdin.write(b"-stay_open\nFalse\n-execute\n")
            self._proc.stdin.flush()
            self._proc.wait(timeout=15)
        except Exception:
            self._proc.kill()
        finally:
            self._proc = None
            self._stderr_thread = None
        logger.info("ExifTool persistent process terminated")

    def __enter__(self) -> "ExifToolWriter":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _drain_stderr(self) -> None:
        assert self._proc is not None
        for line in self._proc.stderr:
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded:
                self._stderr_buf.append(decoded)
                logger.debug("exiftool stderr: %s", decoded)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_cmd_args(
        self,
        file_path: Path,
        fields: dict[str, Any],
        is_first_write: bool,
    ) -> list[str]:
        """Return the ExifTool argument list (excluding the binary name)."""
        args = ["-charset", "filename=UTF8", "-charset", "UTF8"]
        if not (is_first_write and settings.exiftool_write_backup):
            args.append("-overwrite_original")
        # Preserve filesystem mtime — don't let ExifTool bump it on metadata write.
        args.append("-preserve")
        # Clear then set each tag so repeated writebacks never accumulate duplicates.
        for tag, value in fields.items():
            args.append(f"-{tag}=")   # clear existing values
            if isinstance(value, list):
                for v in value:
                    args.append(f"-{tag}={v}")
            else:
                args.append(f"-{tag}={value}")
        args.append(str(file_path))
        return args

    # ------------------------------------------------------------------
    # Write via persistent process
    # ------------------------------------------------------------------

    def _write_persistent(
        self,
        file_path: Path,
        fields: dict[str, Any],
        is_first_write: bool,
    ) -> tuple[bool, str]:
        """Send one file write command to the persistent ExifTool process."""
        assert self._proc is not None
        args = self._build_cmd_args(file_path, fields, is_first_write)
        # Each argument on its own line; -execute signals end of this command.
        payload = "\n".join(args).encode() + b"\n-execute\n"

        with self._lock:
            try:
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()

                # Collect stdout lines until ExifTool signals {ready}.
                output_parts: list[str] = []
                while True:
                    raw = self._proc.stdout.readline()
                    if not raw:
                        self._proc = None
                        return False, "ExifTool process terminated unexpectedly"
                    text = raw.decode("utf-8", errors="replace").rstrip()
                    if text == "{ready}":
                        break
                    if text:
                        output_parts.append(text)

                output = " ".join(output_parts)
                # "0 image files updated" means ExifTool processed it but failed.
                if "0 image files" in output.lower():
                    stderr_hint = "; ".join(self._stderr_buf[-3:]) if self._stderr_buf else ""
                    detail = (output + (" | " + stderr_hint if stderr_hint else "")).strip()
                    return False, detail or "ExifTool write failed"

                return True, output or f"Written to {file_path.name}"

            except BrokenPipeError:
                self._proc = None
                return False, "ExifTool process died (BrokenPipeError)"
            except Exception as exc:
                return False, f"Persistent write error: {exc}"

    # ------------------------------------------------------------------
    # Public write interface
    # ------------------------------------------------------------------

    def write(
        self,
        file_path: Path,
        fields: dict[str, Any],
        dry_run: bool = False,
        is_first_write: bool = True,
    ) -> tuple[bool, str]:
        """
        Write metadata fields into a file.

        Args:
            file_path:      Target RAW file.
            fields:         Dict of ExifTool tag → value(s).
                            List values are written as multi-value tags.
            dry_run:        If True, only returns what would be written — no changes.
            is_first_write: If True, ExifTool creates an _original backup.
                            If False, overwrites without re-backup.

        Returns:
            (success, message)
        """
        if dry_run:
            return True, self._format_dry_run(file_path, fields)

        if not file_path.exists():
            msg = f"File not on disk (may be iCloud stub): {file_path}"
            logger.error(msg)
            return False, msg

        # Use persistent process when active — no Perl startup per file.
        if self._proc is not None:
            return self._write_persistent(file_path, fields, is_first_write)

        # Fallback: one subprocess per file.
        cmd = ["exiftool"] + self._build_cmd_args(file_path, fields, is_first_write)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=settings.exiftool_timeout_sec,
            )
            if result.returncode != 0:
                msg = f"ExifTool error: {result.stderr.strip()}"
                logger.error("Write failed for %s: %s", file_path, msg)
                return False, msg
            logger.info("Written to %s", file_path.name)
            return True, result.stdout.strip()

        except subprocess.TimeoutExpired:
            msg = f"ExifTool timed out writing to {file_path}"
            logger.error(msg)
            return False, msg
        except Exception as e:
            msg = f"Unexpected write error: {e}"
            logger.error(msg)
            return False, msg

    def _format_dry_run(self, file_path: Path, fields: dict[str, Any]) -> str:
        lines = [f"DRY RUN — would write to: {file_path}"]
        for tag, value in fields.items():
            if isinstance(value, list):
                for v in value:
                    lines.append(f"  -{tag}={v}")
            else:
                lines.append(f"  -{tag}={value}")
        return "\n".join(lines)

    def purge_backup(self, file_path: Path) -> bool:
        """Delete the _original backup file created by ExifTool."""
        backup = file_path.with_suffix(file_path.suffix + "_original")
        if backup.exists():
            backup.unlink()
            logger.info("Purged backup: %s", backup)
            return True
        return False

    @staticmethod
    def read_xmp_fields(paths: list[Path]) -> dict[str, dict]:
        """
        Batch-read XMP:PersonInImage and XMP:Subject from a list of files
        using a single ExifTool subprocess call.

        Returns {absolute_path_str: {tag: value_or_list}} for every file
        ExifTool could read.  Missing files or unreadable tags are absent
        from the result — callers should treat that as empty / unknown.
        """
        if not paths:
            return {}
        cmd = [
            "exiftool", "-json", "-fast2",   # -fast2: read only the metadata block, skip full RAW scan
            "-charset", "filename=UTF8", "-q",
            "-XMP:PersonInImage", "-XMP:Subject",
        ] + [str(p) for p in paths]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=120
            )
            if result.returncode != 0 or not result.stdout.strip():
                logger.warning(
                    "ExifTool XMP read failed (rc=%d): %s",
                    result.returncode, result.stderr.decode(errors="replace")[:200],
                )
                return {}
            data: list[dict] = json.loads(result.stdout)
            return {item.get("SourceFile", ""): item for item in data}
        except Exception as exc:
            logger.warning("read_xmp_fields error: %s", exc)
            return {}
