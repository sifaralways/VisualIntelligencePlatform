"""VIP API — Admin / housekeeping routes.

These endpoints allow selective or full reset of the database so the user
can re-run the pipeline without manually touching SQLite.

Scopes (DELETE /api/admin/reset/{scope}):
  all       → wipe every table + all thumbnails on disk
  scan      → reset media_files to 'scanned', drop faces/embeddings/clusters/persons
  faces     → drop faces, embeddings, clusters, persons; reset media_files ingest_state to 'scanned'
  clusters  → drop clusters, persons; unlink face.cluster_id / face.person_id
  persons   → drop persons; unlink cluster.person_id / face.person_id
  thumbs    → delete cached photo thumbnails only (forces regeneration on next scan)
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, HTTPException

from backend.config import settings
from backend.database.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Stats — row counts per table
# ---------------------------------------------------------------------------
@router.get("/stats")
async def get_stats():
    """Return row counts for every major table."""
    async with get_db() as db:
        def _count(table: str):
            return db.execute_fetchall(f"SELECT COUNT(*) as n FROM {table}")

        tables = [
            "media_files", "faces", "embeddings",
            "clusters", "persons", "writeback_queue",
        ]
        result = {}
        for t in tables:
            rows = await db.execute_fetchall(f"SELECT COUNT(*) as n FROM {t}")
            result[t] = rows[0]["n"]

        # Extra detail
        rows = await db.execute_fetchall(
            "SELECT ingest_state, COUNT(*) as n FROM media_files GROUP BY ingest_state"
        )
        result["media_by_state"] = {r["ingest_state"]: r["n"] for r in rows}

        thumb_count = sum(1 for _ in settings.thumbnail_dir.glob("*.jpg"))
        result["thumbnail_files"] = thumb_count

    return result


# ---------------------------------------------------------------------------
# Reset helper
# ---------------------------------------------------------------------------
async def _wipe_thumbnails():
    """Delete both face thumbnails and photo thumbnails."""
    for d in (settings.thumbnail_dir, settings.photo_thumbs_dir):
        if d.exists():
            shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
            logger.info("Wiped thumbnails directory: %s", d)


async def _wipe_photo_thumbs():
    """Delete *only* the cached photo thumbnails (face data untouched)."""
    if settings.photo_thumbs_dir.exists():
        shutil.rmtree(settings.photo_thumbs_dir)
        settings.photo_thumbs_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Wiped photo_thumbs directory")


async def _vacuum_db() -> int:
    """Run VACUUM in a standalone connection (no open transaction allowed).

    Returns the number of pages freed (before - after).
    VACUUM rewrites the entire database file, reclaiming pages freed by
    DELETE FROM.  Without this, SQLite keeps the file at its peak size.
    Must be called OUTSIDE any get_db() block so there is no open writer.
    """
    async with aiosqlite.connect(settings.db_path) as db:
        before = (await (await db.execute("PRAGMA page_count")).fetchone())[0]
        await db.execute("VACUUM")
        after = (await (await db.execute("PRAGMA page_count")).fetchone())[0]
    freed = before - after
    logger.info("VACUUM complete: %d → %d pages (%d pages / %.1f MB freed)",
                before, after, freed, freed * 4096 / 1_048_576)
    return freed


@router.delete("/reset/{scope}")
async def reset(scope: str):
    """Clear data at the specified scope level."""
    valid = {"all", "scan", "faces", "clusters", "persons", "thumbs"}
    if scope not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown scope '{scope}'. Use: {valid}")

    if scope == "all":
        async with get_db() as db:
            # Delete in FK dependency order:
            # writeback_queue → embeddings → faces → clusters → persons → media_files
            await db.execute("DELETE FROM writeback_queue")
            await db.execute("DELETE FROM embeddings")
            await db.execute("DELETE FROM faces")
            await db.execute("UPDATE clusters SET person_id=NULL")
            await db.execute("DELETE FROM clusters")
            await db.execute("DELETE FROM persons")
            await db.execute("DELETE FROM media_files")
            await _wipe_thumbnails()
            logger.warning("ADMIN: Full reset — all tables wiped")
        freed = await _vacuum_db()
        return {"status": "ok", "scope": "all", "detail": "All tables cleared + thumbnails deleted",
                "pages_freed": freed}

    if scope in ("scan", "faces"):
        async with get_db() as db:
            await db.execute("DELETE FROM writeback_queue")
            await db.execute("DELETE FROM embeddings")
            await db.execute("DELETE FROM faces")
            await db.execute("UPDATE clusters SET person_id=NULL")
            await db.execute("DELETE FROM clusters")
            await db.execute("DELETE FROM persons")
            await db.execute("UPDATE media_files SET ingest_state='scanned', needs_reprocess=0")
            await _wipe_thumbnails()
            logger.warning("ADMIN: Faces/scan reset — embeddings, clusters, persons wiped; media_files reset to 'scanned'")
        freed = await _vacuum_db()
        return {"status": "ok", "scope": scope,
                "detail": "All faces, embeddings, clusters and persons cleared. Media scan data kept.",
                "pages_freed": freed}

    if scope == "clusters":
        async with get_db() as db:
            await db.execute("DELETE FROM writeback_queue")
            await db.execute("UPDATE faces SET cluster_id=NULL, person_id=NULL")
            await db.execute("UPDATE clusters SET person_id=NULL")
            await db.execute("DELETE FROM persons")
            await db.execute("DELETE FROM clusters")
            logger.warning("ADMIN: Cluster/person reset — faces and embeddings kept")
        freed = await _vacuum_db()
        return {"status": "ok", "scope": scope,
                "detail": "Clusters and persons cleared. Face embeddings kept — re-run pipeline to re-cluster.",
                "pages_freed": freed}

    if scope == "persons":
        async with get_db() as db:
            await db.execute("DELETE FROM writeback_queue")
            await db.execute("UPDATE faces SET person_id=NULL")
            await db.execute("UPDATE clusters SET person_id=NULL")
            await db.execute("DELETE FROM persons")
            logger.warning("ADMIN: Person reset — clusters kept, persons wiped")
        freed = await _vacuum_db()
        return {"status": "ok", "scope": scope,
                "detail": "All named persons cleared. Clusters remain — re-name them on the People tab.",
                "pages_freed": freed}

    if scope == "thumbs":
        await _wipe_photo_thumbs()
        logger.warning("ADMIN: Photo thumbnails wiped — will regenerate on next pipeline run")
        return {"status": "ok", "scope": scope,
                "detail": "Photo thumbnail cache cleared. Re-run the pipeline scan to regenerate upright thumbnails."}

    # unreachable
    raise HTTPException(status_code=500, detail="Unexpected error")
