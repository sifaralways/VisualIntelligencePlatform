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
import json
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
from backend.ml.tagger import Tagger
from backend.api.websocket import broadcast

try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

logger = logging.getLogger(__name__)

# Module-level singletons — loaded once per process
_detector = FaceDetector()
_embedder = FaceEmbedder()
_faiss = FaissIndex()
_tagger = Tagger()
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

    # -- Phase 4: Tag (objects, animals, geography, places) -----------------
    await _phase_tag()

    # -- Phase 5: Build analysis documents (Rekognition-format JSON) --------
    await _phase_analyse()

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
                    # New file — generate a stable UUID for XMP:Identifier
                    new_vip_id = str(uuid.uuid4())
                    await db.execute("""
                        INSERT INTO media_files
                            (vip_id, file_path, file_hash, file_size, file_format, camera_make, camera_model,
                             date_taken, gps_lat, gps_lon, width, height, is_stub, ingest_state)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'scanned')
                    """, (
                        new_vip_id,
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
# ---------------------------------------------------------------------------
# Photo thumbnail helper (for UI grid)
# ---------------------------------------------------------------------------

def _make_photo_thumb(src: Path, media_id: int) -> Path | None:
    """Resize the preview JPEG to a 600-px wide thumbnail for the UI grid.

    Returns the saved thumbnail path, or None if PIL is unavailable.
    """
    if not _PIL_AVAILABLE:
        return None
    dst = settings.photo_thumbs_dir / f"{media_id}.jpg"
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import ImageOps as _ImageOps
        with _PILImage.open(src) as img:
            # Respect EXIF orientation tag (same reason Finder shows photos upright
            # but naive Image.open does not — the pixels are physically rotated in
            # the file and must be transposed before any resize operation).
            img = _ImageOps.exif_transpose(img)
            img.thumbnail((600, 800), _PILImage.LANCZOS)
            img.save(dst, "JPEG", quality=85, optimize=True)
        return dst
    except Exception:
        return None


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

        # Generate permanent photo thumbnail for the UI grid
        await asyncio.get_event_loop().run_in_executor(
            None, _make_photo_thumb, preview_path, media_id
        )
        faces = await asyncio.get_event_loop().run_in_executor(
            None, _detector.detect, preview_path
        )

        async with get_db() as db:
            for face in faces:
                # Build face_attributes JSON from rich InsightFace data
                face_attrs: dict = {}
                if face.age is not None:
                    face_attrs["AgeRange"] = {
                        "Low":  max(0, face.age - 4),
                        "High": face.age + 4,
                    }
                if face.gender is not None:
                    face_attrs["Gender"] = {"Value": face.gender, "Confidence": 95.0}
                if face.pose_yaw is not None:
                    face_attrs["Pose"] = {
                        "Yaw":   round(face.pose_yaw, 4),
                        "Pitch": round(face.pose_pitch, 4),
                        "Roll":  round(face.pose_roll, 4),
                    }
                if face.landmarks:
                    face_attrs["Landmarks"] = face.landmarks
                if face.quality_brightness is not None:
                    face_attrs["Quality"] = {
                        "Brightness": round(face.quality_brightness, 4),
                        "Sharpness":  round(face.quality_sharpness, 4),
                    }
                # Stubs for attributes needing extra models (filled in a future phase)
                face_attrs.setdefault("Smile",       None)
                face_attrs.setdefault("Eyeglasses",  None)
                face_attrs.setdefault("Sunglasses",  None)
                face_attrs.setdefault("EyesOpen",    None)
                face_attrs.setdefault("MouthOpen",   None)
                face_attrs.setdefault("Beard",       None)
                face_attrs.setdefault("Emotions",    None)
                face_attrs.setdefault("FaceOccluded",None)

                # Insert face record
                cursor = await db.execute("""
                    INSERT INTO faces (media_file_id, bbox_x, bbox_y, bbox_w, bbox_h,
                                       detection_conf, face_attributes)
                    VALUES (?,?,?,?,?,?,?)
                """, (media_id, face.bbox_x, face.bbox_y, face.bbox_w, face.bbox_h,
                      face.detection_conf, json.dumps(face_attrs) if face_attrs else None))
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

        # Previews are kept alive for Phase 4 tagging — deleted there.

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


# ---------------------------------------------------------------------------
# Phase 4: Tag — objects, animals, geography, places
# ---------------------------------------------------------------------------
async def _phase_tag() -> None:
    logger.info("Phase 4: Tagging (objects, animals, geography, places)")
    await broadcast("phase_start", phase="tag")

    # Load tagging models lazily (first pipeline run only)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _tagger.load)

    # Files that have been embedded/clustered but not yet tagged
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT id, file_path, gps_lat, gps_lon
            FROM media_files
            WHERE ingest_state IN ('embedded', 'clustered')
              AND is_stub = 0
        """)

    total = len(rows)
    logger.info("Phase 4: %d files to tag", total)
    if total == 0:
        await broadcast("phase_complete", phase="tag", tagged=0)
        return

    processed = 0
    for row in rows:
        media_id = row["id"]
        file_path = row["file_path"]
        gps_lat = row["gps_lat"]
        gps_lon = row["gps_lon"]
        path = Path(file_path)

        # Preview may have been kept from Phase 2 or needs re-extraction
        preview_path = settings.preview_dir / f"{path.stem}_{_hash_path(path)}.jpg"
        if not preview_path.exists():
            preview_path = await extract_preview(path) or preview_path

        # Run all tagging models
        result = await loop.run_in_executor(
            None, _tagger.tag, preview_path, gps_lat, gps_lon
        )

        # Persist tags to DB
        async with get_db() as db:
            tag_rows: list[tuple] = []

            for label in result.objects:
                tag_rows.append((media_id, "object", label, None, "yolov11"))
            for label in result.animals:
                tag_rows.append((media_id, "animal", label, None, "bioclip"))
            for label in result.geography:
                tag_rows.append((media_id, "geography", label, None, "places365"))
            for label in result.places:
                model = "nominatim" if gps_lat is not None and label == result.places[0] else "clip"
                tag_rows.append((media_id, "place", label, None, model))

            for t in tag_rows:
                await db.execute("""
                    INSERT OR IGNORE INTO media_tags
                        (media_file_id, category, label, confidence, model)
                    VALUES (?,?,?,?,?)
                """, t)

            await db.execute(
                "UPDATE media_files SET ingest_state='tagged' WHERE id=?", (media_id,)
            )

        # Clean up preview now that both Phase 2 and Phase 4 are done
        if preview_path.exists():
            await delete_preview(preview_path)

        processed += 1
        if processed % 20 == 0:
            await broadcast("tag_progress", done=processed, total=total)

    await broadcast("phase_complete", phase="tag", tagged=processed)
    logger.info("Phase 4 complete: %d files tagged", processed)


def _hash_path(path: Path) -> str:
    """Short hash for preview filename (mirrors preview_extractor logic)."""
    import hashlib
    return hashlib.md5(str(path).encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Phase 5: Build analysis documents (Rekognition-format JSON per photo)
# ---------------------------------------------------------------------------
async def _phase_analyse() -> None:
    logger.info("Phase 5: Building analysis documents")
    await broadcast("phase_start", phase="analyse")

    from backend.ml.analysis_builder import (
        build_analysis_document, save_analysis_document, MODEL_VERSION
    )

    # All tagged files that either have no analysis doc yet, or whose
    # model_version has changed (i.e. models were upgraded).
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT mf.id
            FROM media_files mf
            LEFT JOIN photo_analysis pa ON pa.media_file_id = mf.id
            WHERE mf.ingest_state = 'tagged'
              AND mf.is_stub = 0
              AND (pa.id IS NULL OR pa.model_version != ?)
        """, (MODEL_VERSION,))

    total = len(rows)
    logger.info("Phase 5: %d files need analysis docs", total)
    if total == 0:
        await broadcast("phase_complete", phase="analyse", analysed=0)
        return

    processed = 0
    for row in rows:
        media_id = row["id"]
        try:
            async with get_db() as db:
                doc = await build_analysis_document(media_id, db)
                await save_analysis_document(media_id, doc, db)
        except Exception as e:
            logger.error("Analysis doc failed for media_id=%d: %s", media_id, e)
            continue

        processed += 1
        if processed % 50 == 0:
            await broadcast("analyse_progress", done=processed, total=total)

    await broadcast("phase_complete", phase="analyse", analysed=processed)
    logger.info("Phase 5 complete: %d analysis docs built/updated", processed)
