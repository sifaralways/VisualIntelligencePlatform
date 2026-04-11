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

import asyncio
import base64
import io
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import aiosqlite
import numpy as np
from fastapi import APIRouter, HTTPException
from PIL import Image
from pydantic import BaseModel

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


# ---------------------------------------------------------------------------
# Contacts Face Match
# ---------------------------------------------------------------------------

class ContactsMatchRequest(BaseModel):
    threshold: float = 0.60


def _extract_photo_from_vcard(vcard_text: str) -> bytes | None:
    """Parse a vCard block and return the embedded PHOTO as raw bytes.

    Handles both vCard 3.0 (ENCODING=b) and vCard 4.0 (data URI) formats.
    Lines are unfolded per RFC 6350 §3.2 before parsing.
    """
    lines = vcard_text.replace("\r\n", "\n").split("\n")
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    b64: str | None = None
    for line in unfolded:
        upper = line.upper()
        if upper.startswith("PHOTO"):
            if "BASE64," in upper or "DATA:" in upper:
                # vCard 4.0: PHOTO:data:image/jpeg;base64,<b64>
                _, _, after = line.partition(",")
                b64 = after.strip()
                break
            if "ENCODING=B" in upper or "ENCODING=BASE64" in upper:
                # vCard 3.0: PHOTO;ENCODING=b;TYPE=JPEG:<b64>
                _, _, after = line.partition(":")
                b64 = after.strip()
                break

    if not b64:
        return None
    try:
        return base64.b64decode(b64 + "==")
    except Exception:
        return None


def _export_contacts_sync() -> list[dict]:
    """Export contacts with photos from macOS Contacts via AppleScript.

    Returns list of {name: str, image_bytes: bytes}.
    Runs synchronously — must be called from a thread executor.
    """
    script = """
tell application "Contacts"
    set result_list to {}
    repeat with p in every person
        set hasImg to false
        try
            set imgData to image of p
            if imgData is not missing value then
                set hasImg to true
            end if
        end try
        if hasImg then
            set pName to (name of p) as text
            set vcText to vcard of p
            set end of result_list to pName & "~~SEP~~" & vcText & "~~END~~"
        end if
    end repeat
    return result_list as text
end tell
"""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        logger.warning("AppleScript contacts export error: %s", result.stderr.strip())
        return []

    raw = result.stdout.strip()
    if not raw:
        return []

    contacts: list[dict] = []
    for record in raw.split("~~END~~"):
        record = record.strip()
        if "~~SEP~~" not in record:
            continue
        name, _, vcard_text = record.partition("~~SEP~~")
        img = _extract_photo_from_vcard(vcard_text)
        if img is not None:
            contacts.append({"name": name.strip(), "image_bytes": img})

    return contacts


def _contacts_match_in_thread(
    clusters: list[dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    """CPU-bound: export contacts, embed them, match against cluster centroids.

    Designed to run inside asyncio.run_in_executor so it does not block the
    event loop.  ``clusters`` is pre-loaded from the DB by the async caller.

    Returns:
        {matches, total_contacts, contacts_with_face}
    """
    import backend.database.settings_store as _ss
    from backend.ml.face_detector import FaceDetector

    # 1. Export contacts
    contacts = _export_contacts_sync()
    total_contacts = len(contacts)
    if not contacts:
        return {"matches": [], "total_contacts": 0, "contacts_with_face": 0}

    # 2. Load InsightFace model (accuracy mode: CPU 1280)
    asyncio.run(_ss.load_cache())
    _ss._cache["face_detection_mode"] = 0
    detector = FaceDetector()
    detector.load()

    # 3. Embed each contact photo
    embedded: list[dict[str, Any]] = []
    for c in contacts:
        emb: np.ndarray | None = None
        try:
            img_arr = np.array(Image.open(io.BytesIO(c["image_bytes"])).convert("RGB"))
            emb = detector.embed_from_array(img_arr)
        except Exception as exc:
            logger.debug("embed_contact failed for %s: %s", c["name"], exc)
        embedded.append({"name": c["name"], "embedding": emb})

    with_face = [e for e in embedded if e["embedding"] is not None]
    contacts_with_face = len(with_face)

    # 4. Match each contact embedding against cluster centroids
    matches: list[dict[str, Any]] = []
    for contact in with_face:
        emb = contact["embedding"]
        best_sim, best_cluster = -1.0, None
        for cluster in clusters:
            sim = float(np.dot(emb, cluster["centroid"]))
            if sim > best_sim:
                best_sim, best_cluster = sim, cluster
        if best_cluster is not None and best_sim >= threshold:
            matches.append({
                "contact_name":   contact["name"],
                "cluster_id":     best_cluster["cluster_id"],
                "cluster_size":   best_cluster["member_count"],
                "similarity_pct": round(best_sim * 100, 1),
                "auto_name":      best_sim >= 0.90,
                "thumbnail_path": best_cluster["rep_thumb"],
            })

    matches.sort(key=lambda r: r["similarity_pct"], reverse=True)
    return {
        "matches":            matches,
        "total_contacts":     total_contacts,
        "contacts_with_face": contacts_with_face,
    }


@router.post("/contacts-match")
async def contacts_match(req: ContactsMatchRequest):
    """Match macOS Contacts photos against unnamed face clusters in VIP.

    Exports contact photos via AppleScript, embeds them with InsightFace, and
    compares against all unnamed cluster centroids using cosine similarity.

    This is a read-only diagnostic — no data is written to the database.
    Accepts via POST /persons/from-cluster/{cluster_id} on the persons route.
    """
    threshold = max(0.30, min(0.99, req.threshold))

    # Load unnamed cluster centroids (async DB query)
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT c.id AS cluster_id, c.member_count, c.centroid,
                   MIN(f.thumbnail_path) AS rep_thumb
            FROM clusters c
            LEFT JOIN faces f ON f.cluster_id = c.id AND f.thumbnail_path IS NOT NULL
            WHERE c.person_id IS NULL
            GROUP BY c.id
            ORDER BY c.member_count DESC
        """)

    clusters: list[dict[str, Any]] = []
    for row in rows:
        if not row["centroid"]:
            continue
        try:
            arr = np.frombuffer(row["centroid"], dtype=np.float32).copy()
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr /= norm
            clusters.append({
                "cluster_id":   row["cluster_id"],
                "member_count": row["member_count"],
                "centroid":     arr,
                "rep_thumb":    row["rep_thumb"] or "",
            })
        except Exception:
            continue

    if not clusters:
        return {
            "matches": [],
            "stats": {
                "total_contacts": 0,
                "contacts_with_face": 0,
                "unnamed_clusters": 0,
                "elapsed_seconds": 0,
            },
        }

    t0 = time.time()
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, _contacts_match_in_thread, clusters, threshold
        )
    except Exception as exc:
        logger.exception("contacts_match failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    elapsed = round(time.time() - t0, 1)
    logger.info(
        "contacts_match: %d contacts / %d with face / %d clusters / %d matches ≥%.0f%% (%.1fs)",
        result["total_contacts"], result["contacts_with_face"],
        len(clusters), len(result["matches"]), threshold * 100, elapsed,
    )

    return {
        "matches": result["matches"],
        "stats": {
            "total_contacts":     result["total_contacts"],
            "contacts_with_face": result["contacts_with_face"],
            "unnamed_clusters":   len(clusters),
            "elapsed_seconds":    elapsed,
        },
    }

