"""
VIP database layer — aiosqlite connection management and initialisation.

Usage:
  from backend.database.db import get_db, init_db

  async with get_db() as db:
      rows = await db.execute_fetchall("SELECT * FROM media_files")

CLI usage (called by setup.sh):
  python -m backend.database.db --init
"""

import asyncio
import logging
import sys
import uuid as _uuid_mod
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite

from backend.config import settings, ensure_dirs

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


# ---------------------------------------------------------------------------
# Connection context manager
# ---------------------------------------------------------------------------
@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Yield a single aiosqlite connection.  Handles commit/rollback."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row  # access columns by name
        await db.execute("PRAGMA journal_mode=WAL")    # concurrent reads
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA synchronous=NORMAL")  # safe + fast
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# ---------------------------------------------------------------------------
# Initialisation — run all migrations in order
# ---------------------------------------------------------------------------
async def init_db() -> None:
    """Create tables and run pending migrations."""
    ensure_dirs()
    logger.info("Initialising database at %s", settings.db_path)

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        logger.warning("No migration files found in %s", MIGRATIONS_DIR)
        return

    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        # Create migrations tracking table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                id          INTEGER PRIMARY KEY,
                filename    TEXT NOT NULL UNIQUE,
                applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.commit()

        applied = {
            row[0]
            for row in await db.execute_fetchall("SELECT filename FROM _migrations")
        }

        for migration_file in migration_files:
            if migration_file.name in applied:
                logger.debug("Migration already applied: %s", migration_file.name)
                continue

            logger.info("Applying migration: %s", migration_file.name)
            sql = migration_file.read_text()
            await db.executescript(sql)
            await db.execute(
                "INSERT INTO _migrations (filename) VALUES (?)",
                (migration_file.name,),
            )
            await db.commit()
            logger.info("✅  Applied: %s", migration_file.name)

        # Backfill vip_id for any existing rows that pre-date migration 003
        null_rows = await db.execute_fetchall(
            "SELECT id FROM media_files WHERE vip_id IS NULL"
        )
        if null_rows:
            logger.info("Backfilling vip_id for %d existing media rows…", len(null_rows))
            for r in null_rows:
                await db.execute(
                    "UPDATE media_files SET vip_id=? WHERE id=?",
                    (str(_uuid_mod.uuid4()), r[0]),
                )
            await db.commit()
            logger.info("vip_id backfill complete")

        # Backfill asset_id for rows that pre-date migration 026 or were
        # created before the app upgrade.  Prefer vip_id for continuity.
        asset_null_rows = await db.execute_fetchall(
            "SELECT id, vip_id FROM media_files WHERE asset_id IS NULL"
        )
        if asset_null_rows:
            logger.info("Backfilling asset_id for %d existing media rows…", len(asset_null_rows))
            for r in asset_null_rows:
                seed = r[1] if r[1] else str(_uuid_mod.uuid4())
                await db.execute(
                    "UPDATE media_files SET asset_id=? WHERE id=?",
                    (seed, r[0]),
                )
            await db.commit()
            logger.info("asset_id backfill complete")

    logger.info("Database ready.")


# ---------------------------------------------------------------------------
# CLI entry point (called by setup.sh)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--init" in sys.argv:
        asyncio.run(init_db())
    else:
        print("Usage: python -m backend.database.db --init")
        sys.exit(1)
