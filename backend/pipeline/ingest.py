"""
VIP Pipeline — main ingest orchestrator.

Coordinates: walk → hash → EXIF → preview extract → detect → embed → cluster

Design:
  - Each stage is async and emits progress via WebSocket
  - Idempotency enforced at every step
  - Crash-safe: each completed step is committed to DB before the next
  - Thermal-aware: sleeps between batches if needed
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional

import aiosqlite

from backend.config import settings
from backend.database.db import get_db
from backend.scanner.walker import walk_folder
from backend.scanner.hasher import compute_hash, check_idempotency
from backend.scanner.exif_reader import ExifToolReader
from backend.scanner.preview_extractor import extract_preview, delete_preview
from backend.ml.face_detector import FaceDetector
from backend.ml.embedder import FaceEmbedder, save_face_thumbnail
from backend.ml.clusterer import cluster_embeddings
from backend.ml.index import FaissIndex
from backend.api.websocket import broadcast

logger = logging.getLogger(__name__)

# Module-level singletons — loaded once per process
_detector = FaceDetector()
_embedder = FaceEmbedder()
_faiss = FaissIndex()
_models_loaded = False


def _ensure_models() -> None:
    global _models_loaded
    if not _models_loaded:
        _detector.load()
        # _embedder.load() not needed — InsightFace computes embeddings
        # during detection (face.normed_embedding). FaceEmbedder is only
        # used here for vector_to_bytes / bytes_to_vector utility methods.
        _faiss.load()
        _models_loaded = True


# ---------------------------------------------------------------------------
# Main pipeline entry point
# ---------------------------------------------------------------------------
async def run_ingest(folder: str) -> None:
    """
    Full ingest pipeline for a given folder path.
    Called by the API route /api/pipeline/scan
    """
    folder_path = Path(folder).resolve()
    logger.info("=== Pipeline start: %s ===", folder_path)

    await broadcast("pipeline_start", folder=str(folder_path))

    _ensure_models()

    # -- Phase 1: Scan, hash, EXIF -----------------------------------------
    await _phase_scan(folder_path)

    # -- Phase 2: Extract previews + detect + embed -------------------------
    await _phase_embed()

    # -- Phase 3: Cluster ---------------------------------------------------
    await _phase_cluster()

    await broadcast("pipeline_complete", folder=str(folder_path))
    logger.info("=== Pipeline complete: %s ===", folder_path)


# ---------------------------------------------------------------------------
# Phase 1: Scan
# ---------------------------------------------------------------------------
async def _phase_scan(folder: Path) -> None:
    logger.info("Phase 1: Scanning %s", folder)
    await broadcast("phase_start", phase="scan")

    scanned = 0
    skipped = 0

    async with ExifToolReader() as exif:
        async for file_path in walk_folder(folder):
            async with get_db() as db:
                # 1a. Hash + idempotency
                file_hash = await asyncio.get_event_loop().run_in_executor(
                    None, compute_hash, file_path
                )
                skip, existing_id = await check_idempotency(db, file_hash, str(file_path))

                if skip:
                    skipped += 1
                    continue

                # 1b. File stats
                stat = file_path.stat()

                # 1c. EXIF metadata
                meta = await exif.read(file_path)

                # 1d. iCloud stub check
                is_stub = stat.st_size < settings.stub_max_size_bytes

                if existing_id:
                    # Re-evaluation: update existing record
                    await db.execute("""
                        UPDATE media_files SET
                            file_path=?, file_size=?, file_format=?, camera_make=?, camera_model=?,
                            date_taken=?, gps_lat=?, gps_lon=?, width=?, height=?,
                            is_stub=?, ingest_state='scanned', needs_reprocess=0,
                            last_seen_at=datetime('now')
                        WHERE id=?
                    """, (
                        str(file_path), stat.st_size, meta.get("file_format"),
                        meta.get("camera_make"), meta.get("camera_model"),
                        meta.get("date_taken"), meta.get("gps_lat"), meta.get("gps_lon"),
                        meta.get("width"), meta.get("height"), int(is_stub), existing_id,
                    ))
                else:
                    # New file
                    await db.execute("""
                        INSERT INTO media_files
                            (file_path, file_hash, file_size, file_format, camera_make, camera_model,
                             date_taken, gps_lat, gps_lon, width, height, is_stub, ingest_state)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'scanned')
                    """, (
                        str(file_path), file_hash, stat.st_size, meta.get("file_format"),
                        meta.get("camera_make"), meta.get("camera_model"),
                        meta.get("date_taken"), meta.get("gps_lat"), meta.get("gps_lon"),
                        meta.get("width"), meta.get("height"), int(is_stub),
                    ))

                scanned += 1

            if scanned % 100 == 0:
                await broadcast("scan_progress", done=scanned, skipped=skipped)

    await broadcast("phase_complete", phase="scan", scanned=scanned, skipped=skipped)
    logger.info("Phase 1 complete: %d scanned, %d skipped", scanned, skipped)


# ---------------------------------------------------------------------------
# Phase 2: Extract previews, detect faces, generate embeddings
# ---------------------------------------------------------------------------
async def _phase_embed() -> None:
    logger.info("Phase 2: Embedding faces")
    await broadcast("phase_start", phase="embed")

    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id, file_path FROM media_files WHERE ingest_state='scanned' AND is_stub=0"
        )

    total = len(rows)
    logger.info("%d files to embed", total)
    processed = 0

    for row in rows:
        media_id, file_path = row["id"], row["file_path"]
        path = Path(file_path)

        if not path.exists():
            logger.warning("File not on disk (possibly offloaded): %s", file_path)
            continue

        # Extract embedded JPEG preview
        preview_path = await extract_preview(path)
        if preview_path is None:
            async with get_db() as db:
                await db.execute(
                    "UPDATE media_files SET ingest_state='embedded' WHERE id=?", (media_id,)
                )
            continue

        # Detect faces
        faces = await asyncio.get_event_loop().run_in_executor(
            None, _detector.detect, preview_path
        )

        async with get_db() as db:
            for face in faces:
                # Insert face record
                cursor = await db.execute("""
                    INSERT INTO faces (media_file_id, bbox_x, bbox_y, bbox_w, bbox_h, detection_conf)
                    VALUES (?,?,?,?,?,?)
                """, (media_id, face.bbox_x, face.bbox_y, face.bbox_w, face.bbox_h, face.detection_conf))
                face_id = cursor.lastrowid

                # Save thumbnail
                thumb_path = save_face_thumbnail(face.crop, face_id)
                await db.execute(
                    "UPDATE faces SET thumbnail_path=? WHERE id=?",
                    (str(thumb_path), face_id)
                )

                # Embedding — InsightFace already computed it during detection.
                # face.embedding is the 512-D normed ArcFace vector.
                vector = face.embedding
                if vector is not None:
                    await db.execute("""
                        INSERT INTO embeddings (face_id, vector, model_version)
                        VALUES (?,?,?)
                    """, (face_id, _embedder.vector_to_bytes(vector), _embedder.model_version))

            await db.execute(
                "UPDATE media_files SET ingest_state='embedded' WHERE id=?", (media_id,)
            )

        # Clean up temp preview
        await delete_preview(preview_path)

        processed += 1
        if processed % 50 == 0:
            await broadcast("embed_progress", done=processed, total=total)
            # Thermal-aware pause
            await asyncio.sleep(settings.thermal_sleep_sec)

    await broadcast("phase_complete", phase="embed", processed=processed)
    logger.info("Phase 2 complete: %d files processed", processed)


# ---------------------------------------------------------------------------
# Phase 3: Cluster all embeddings
# ---------------------------------------------------------------------------
async def _phase_cluster() -> None:
    logger.info("Phase 3: Clustering")
    await broadcast("phase_start", phase="cluster")

    # Step 1: Wipe unnamed clusters so their faces rejoin the pool.
    # This ensures new arrivals get context from ALL unowned faces, not just
    # themselves — critical when a single new photo brings few new faces.
    async with get_db() as db:
        unnamed = await db.execute_fetchall(
            "SELECT id FROM clusters WHERE person_id IS NULL"
        )
        unnamed_ids = [r["id"] for r in unnamed]
        if unnamed_ids:
            ph = ",".join("?" * len(unnamed_ids))
            # Release face → cluster assignments for unnamed clusters
            await db.execute(
                f"UPDATE faces SET cluster_id=NULL WHERE cluster_id IN ({ph})",
                unnamed_ids,
            )
            await db.execute(
                f"DELETE FROM clusters WHERE id IN ({ph})",
                unnamed_ids,
            )
            logger.info("Cleared %d unnamed clusters; faces returned to pool", len(unnamed_ids))

    # Step 2: Gather ALL faces not yet owned by a named person
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT f.id as face_id, e.vector
            FROM faces f
            JOIN embeddings e ON e.face_id = f.id
            WHERE f.cluster_id IS NULL
        """)

    if not rows:
        logger.info("No unclustered embeddings")
        return

    face_ids = [r["face_id"] for r in rows]
    import numpy as np
    vectors = [FaceEmbedder.bytes_to_vector(r["vector"]) for r in rows]

    results = cluster_embeddings(face_ids, vectors)

    async with get_db() as db:
        for cluster in results:
            cursor = await db.execute("""
                INSERT INTO clusters (centroid, member_count, intra_similarity, is_high_conf)
                VALUES (?,?,?,?)
            """, (
                cluster.centroid.tobytes(),
                cluster.member_count,
                cluster.intra_similarity,
                int(cluster.is_high_conf),
            ))
            cluster_id = cursor.lastrowid

            for face_id in cluster.face_ids:
                await db.execute(
                    "UPDATE faces SET cluster_id=? WHERE id=?", (cluster_id, face_id)
                )

            # Update media_files state
            await db.execute("""
                UPDATE media_files SET ingest_state='clustered'
                WHERE id IN (SELECT media_file_id FROM faces WHERE cluster_id=?)
            """, (cluster_id,))

    # Rebuild FAISS index
    _faiss.build(face_ids, vectors)
    _faiss.save()

    await broadcast("phase_complete", phase="cluster", clusters=len(results))
    logger.info("Phase 3 complete: %d clusters", len(results))
