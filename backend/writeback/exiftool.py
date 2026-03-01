"""
VIP Writeback — ExifTool subprocess wrapper.

Principle: ExifTool writes atomically.
  1. ExifTool writes to a temp file first.
  2. If successful, renames temp → original.
  3. A crash mid-write leaves the original untouched.
  4. On first write: a _original backup is created automatically.
  5. On subsequent rewrites: -overwrite_original skips re-backup.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)


class ExifToolWriter:
    """Writes XMP metadata into RAW files via ExifTool CLI."""

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

        cmd = ["exiftool", "-charset", "filename=UTF8", "-charset", "UTF8"]

        # Backup flag
        if is_first_write and settings.exiftool_write_backup:
            pass  # ExifTool backs up by default (creates _original)
        else:
            cmd.append("-overwrite_original")

        # Preserve filesystem modification timestamp — do not let ExifTool
        # change the file's mtime when writing metadata.
        cmd.append("-preserve")

        # Build tag arguments.
        # IMPORTANT: clear each tag first (e.g. -TAG=) before setting new values
        # so that repeated writeback runs never accumulate duplicates.
        for tag, value in fields.items():
            cmd.append(f"-{tag}=")   # clear existing values for this tag
            if isinstance(value, list):
                for v in value:
                    cmd.append(f"-{tag}={v}")
            else:
                cmd.append(f"-{tag}={value}")

        cmd.append(str(file_path))

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
