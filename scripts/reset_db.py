#!/usr/bin/env python3
"""
VIP Dev Tool — Reset database and FAISS index.

Wipes all processed data while preserving the names you've already assigned.
Useful when re-running the pipeline after model changes.

Usage:
    python scripts/reset_db.py            # wipe DB + FAISS, keep person names export
    python scripts/reset_db.py --hard     # wipe everything including names
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.config import settings
from backend.database.db import get_db, init_db


async def export_names() -> list[dict]:
    """Export all named persons before wipe."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT uuid, name FROM persons WHERE name IS NOT NULL AND is_merged=0"
        )
    return [{"uuid": r["uuid"], "name": r["name"]} for r in rows]


async def reset(hard: bool = False) -> None:
    export_path = settings.app_support_dir / "persons_backup.json"

    if not hard:
        names = await export_names()
        with open(export_path, "w") as f:
            json.dump(names, f, indent=2)
        print(f"✅  Exported {len(names)} person names → {export_path}")

    # Drop DB
    if settings.db_path.exists():
        settings.db_path.unlink()
        print(f"🗑️   Deleted: {settings.db_path}")

    # Drop FAISS index
    for p in [settings.faiss_path, settings.faiss_path.with_suffix(".ids.pkl")]:
        if p.exists():
            p.unlink()
            print(f"🗑️   Deleted: {p}")

    # Clear thumbnails and previews
    for d in [settings.thumbnail_dir, settings.preview_dir]:
        if d.exists():
            import shutil
            shutil.rmtree(d)
            d.mkdir()
            print(f"🗑️   Cleared: {d}")

    # Re-init DB schema
    await init_db()
    print("✅  Database re-initialised")

    if not hard and export_path.exists():
        print(f"\nℹ️   Person names backed up. Re-import after re-run with:")
        print(f"    python scripts/reset_db.py --restore {export_path}")


async def restore_names(backup_path: Path) -> None:
    with open(backup_path) as f:
        names: list[dict] = json.load(f)

    import uuid as _uuid
    async with get_db() as db:
        for entry in names:
            await db.execute("""
                INSERT OR IGNORE INTO persons (uuid, name, named_at)
                VALUES (?, ?, datetime('now'))
            """, (entry["uuid"], entry["name"]))

    print(f"✅  Restored {len(names)} person names from {backup_path}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="VIP database reset tool")
    parser.add_argument("--hard", action="store_true", help="Wipe everything including named persons")
    parser.add_argument("--restore", type=Path, help="Restore names from backup JSON")
    args = parser.parse_args()

    if args.restore:
        await restore_names(args.restore)
    else:
        print("\n⚠️   This will delete all processed data" + (" including names" if args.hard else "") + ".")
        confirm = input("Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return
        await reset(hard=args.hard)


if __name__ == "__main__":
    asyncio.run(main())
