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
import numpy as np

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
from backend.database import settings_store

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
# Reprocess entry point (no filesystem scan — works on existing DB photos)
# ---------------------------------------------------------------------------
async def run_reprocess() -> None:
    """
    Re-evaluate quality signals and auto-merge suggestions for every photo
    already in the library.  Does NOT re-scan the filesystem, re-detect
    faces, or disturb cluster / person assignments.

    Steps:
      1. Re-run blur + closed-eyes detection on each photo thumbnail
      2. Re-run Phase 3b auto-merge (surface new merge suggestions)
      3. Rebuild analysis documents so quality flags appear in the UI
    """
    logger.info("=== Reprocess start ===")
    await broadcast("pipeline_start", folder="[library reprocess]")
    await settings_store.load_cache()
    _ensure_models()

    # Step 1: quality re-check on stored photo thumbnails
    await broadcast("phase_start", phase="quality_recheck")
    async with get_db() as db:
        media_rows = await db.execute_fetchall(
            "SELECT id, exposure_time_s FROM media_files WHERE is_stub=0"
        )

    from backend.ml.quality_checker import score_blur, classify_blur

    updated = 0
    for row in media_rows:
        media_id = row["id"]
        _raw_exp = row["exposure_time_s"]
        try:
            exp_s = float(_raw_exp) if _raw_exp is not None else None
        except (ValueError, TypeError):
            exp_s = None
        thumb    = settings.photo_thumbs_dir / f"{media_id}.jpg"
        if not thumb.exists():
            continue
        try:
            thumb_arr = np.array(_PILImage.open(thumb).convert("L"))
            blur_score = score_blur(thumb_arr)
            is_blurry, long_exp = classify_blur(blur_score, exp_s)
        except Exception as _e:
            logger.debug("Quality recheck failed for media %d: %s", media_id, _e)
            continue

        # Derive has_closed_eyes from existing face_attributes
        has_closed_eyes = 0
        try:
            async with get_db() as db:
                face_rows = await db.execute_fetchall(
                    "SELECT face_attributes FROM faces "
                    "WHERE media_file_id=? AND face_attributes IS NOT NULL",
                    (media_id,),
                )
            for fr in face_rows:
                attrs = json.loads(fr["face_attributes"])
                eyes  = attrs.get("EyesOpen")
                if isinstance(eyes, dict) and eyes.get("Value") is False:
                    has_closed_eyes = 1
                    break
        except Exception:
            pass

        async with get_db() as db:
            await db.execute(
                "UPDATE media_files "
                "SET blur_score=?, is_blurry=?, long_exposure=?, has_closed_eyes=? "
                "WHERE id=?",
                (blur_score, is_blurry, long_exp, has_closed_eyes, media_id),
            )
        updated += 1

    logger.info("Quality recheck: %d photos updated", updated)
    await broadcast("phase_complete", phase="quality_recheck", processed=updated)

    # Notify frontend if any quality issues exist in the library
    async with get_db() as db:
        _q = await (await db.execute(
            "SELECT COUNT(*) AS cnt FROM media_files WHERE is_blurry=1 OR has_closed_eyes=1"
        )).fetchone()
    if _q and _q["cnt"] > 0:
        await broadcast("quality_issues_found", count=_q["cnt"])

    # Step 2: auto-merge check
    await _phase_auto_merge()

    # Step 3: rebuild analysis docs so new quality info appears
    await _phase_analyse()

    await broadcast("pipeline_complete", folder="[library reprocess]")
    logger.info("=== Reprocess complete ===")


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

    # Refresh the in-process settings cache from DB so any tweaks made in
    # the Admin UI take effect on the next pipeline run without a restart.
    await settings_store.load_cache()

    _ensure_models()

    # -- Phase 1: Scan, hash, EXIF -----------------------------------------
    await _phase_scan(folder_path)

    # -- Phase 2: Extract previews + detect + embed -------------------------
    await _phase_embed()

    # Notify the frontend if quality issues were found during embed
    async with get_db() as db:
        _q = await (await db.execute("""
            SELECT COUNT(*) AS cnt FROM media_files
            WHERE is_blurry = 1 OR has_closed_eyes = 1
        """)).fetchone()
    _quality_count = _q["cnt"] if _q else 0
    if _quality_count > 0:
        await broadcast("quality_issues_found", count=_quality_count)

    # -- Phase 3: Cluster ---------------------------------------------------
    await _phase_cluster()

    # -- Phase 3b: Auto-merge high-conf + surface borderline suggestions ----
    await _phase_auto_merge()

    # -- Phase 4: Tag (objects, animals, geography, places) -----------------
    await _phase_tag()

    # -- Phase 5: Build analysis documents (Rekognition-format JSON) --------
    await _phase_analyse()

    # Sync denormalised photo_count on all active persons
    async with get_db() as db:
        await db.execute("""
            UPDATE persons
            SET photo_count = (
                SELECT COUNT(DISTINCT f.media_file_id)
                FROM faces f
                WHERE f.person_id = persons.id
            )
            WHERE is_merged = 0
        """)
    logger.info("Synced photo_count for all persons")

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
                    # Re-evaluation: update existing record (also clears removed_from_app)
                    await db.execute("""
                        UPDATE media_files SET
                            file_path=?, file_size=?, file_format=?, camera_make=?, camera_model=?,
                            date_taken=?, gps_lat=?, gps_lon=?, width=?, height=?,
                            is_stub=?, exposure_time_s=?,
                            ingest_state='scanned', needs_reprocess=0, removed_from_app=0,
                            last_seen_at=datetime('now')
                        WHERE id=?
                    """, (
                        str(file_path), stat.st_size, meta.get("file_format"),
                        meta.get("camera_make"), meta.get("camera_model"),
                        meta.get("date_taken"), meta.get("gps_lat"), meta.get("gps_lon"),
                        meta.get("width"), meta.get("height"), int(is_stub),
                        meta.get("exposure_time_s"), existing_id,
                    ))
                else:
                    # New file — use file's existing XMP:Identifier if present
                    # (preserves continuity when re-importing a file previously
                    # processed by VIP on another machine / after DB reset).
                    # Fall back to a fresh UUID4 for files VIP hasn't seen before.
                    new_vip_id = meta.get("xmp_identifier") or str(uuid.uuid4())

                    # Build a one-time snapshot of whatever rich XMP/IPTC data
                    # existed in the file *before* VIP touches it.  This snapshot
                    # drives the "VIP History" / "External History" display.
                    _ext: dict = {}
                    if meta.get("xmp_identifier"):
                        _ext["identifier"] = meta["xmp_identifier"]
                    if meta.get("xmp_persons"):
                        _ext["persons"] = meta["xmp_persons"]
                    if meta.get("xmp_keywords"):
                        _ext["keywords"] = meta["xmp_keywords"]
                    if meta.get("xmp_location"):
                        _ext["location"] = meta["xmp_location"]
                    if meta.get("xmp_region_info"):
                        _ext["region_info"] = meta["xmp_region_info"]
                    external_exif_json = json.dumps(_ext) if _ext else None

                    await db.execute("""
                        INSERT INTO media_files
                            (vip_id, file_path, file_hash, file_size, file_format, camera_make, camera_model,
                             date_taken, gps_lat, gps_lon, width, height, is_stub, exposure_time_s,
                             ingest_state, external_exif)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'scanned',?)
                    """, (
                        new_vip_id,
                        str(file_path), file_hash, stat.st_size, meta.get("file_format"),
                        meta.get("camera_make"), meta.get("camera_model"),
                        meta.get("date_taken"), meta.get("gps_lat"), meta.get("gps_lon"),
                        meta.get("width"), meta.get("height"), int(is_stub),
                        meta.get("exposure_time_s"),
                        external_exif_json,
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

        # ── Skip re-embedding if face embeddings already exist (re-scan case) ──
        # When a file is removed then re-added, its ingest_state is reset to
        # 'scanned' (see hasher.py / ingest.py UPDATE path), but the old face
        # rows — with their cluster_id / person_id intact — still exist in the
        # DB.  Re-running detection would INSERT duplicate face rows and then
        # the cluster phase would create duplicate persons.  Instead, just
        # advance ingest_state and leave the existing faces untouched.
        async with get_db() as db:
            _emb_check = await db.execute_fetchall(
                """SELECT 1 FROM faces f
                   JOIN embeddings e ON e.face_id = f.id
                   WHERE f.media_file_id = ?
                   LIMIT 1""",
                (media_id,),
            )
        if _emb_check:
            async with get_db() as db:
                await db.execute(
                    "UPDATE media_files SET ingest_state='embedded' WHERE id=?", (media_id,)
                )
            processed += 1
            continue

        # ── Blur detection on full preview (must happen before thumbnail resize) ──
        blur_score: float | None = None
        is_blurry: int | None = None
        long_exposure_flag: int | None = None
        exp_s: float | None = None  # exposure time read here, reused for face-blur override
        try:
            from backend.ml.quality_checker import score_blur, classify_blur
            preview_arr = np.array(_PILImage.open(preview_path).convert("L"))  # greyscale
            # Read exposure_time_s from DB (set during Phase 1 scan)
            async with get_db() as db:
                exp_row = await (await db.execute(
                    "SELECT exposure_time_s FROM media_files WHERE id=?", (media_id,)
                )).fetchone()
            _raw_exp = exp_row["exposure_time_s"] if exp_row else None
            try:
                exp_s = float(_raw_exp) if _raw_exp is not None else None
            except (ValueError, TypeError):
                exp_s = None
            blur_score = score_blur(preview_arr)
            is_blurry, long_exposure_flag = classify_blur(blur_score, exp_s)
        except Exception as _blur_exc:
            logger.debug("Blur check failed for %s: %s", file_path, _blur_exc)

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

                # Gate gender/age on sharpness: the GenderAge sub-model produces
                # near-random results on blurry or tiny crops.  quality_sharpness
                # is a Laplacian-variance proxy normalised to 0–100.
                _sharp_enough = (face.quality_sharpness or 0) >= settings_store.get('gender_min_sharpness')
                if face.age is not None and _sharp_enough:
                    face_attrs["AgeRange"] = {
                        "Low":  max(0, face.age - 4),
                        "High": face.age + 4,
                    }
                if face.gender is not None and _sharp_enough:
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
                # EyesOpen: now computed from vertical gradient of eye-region patch
                if face.eyes_open is not None:
                    face_attrs["EyesOpen"] = {
                        "Value": face.eyes_open,
                        "Confidence": 80.0,  # heuristic, not a calibrated model
                    }
                else:
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

        # ── Derive has_closed_eyes from the faces we just inserted ─────────────
        has_closed_eyes = 0
        try:
            async with get_db() as db:
                face_attr_rows = await db.execute_fetchall(
                    "SELECT face_attributes FROM faces WHERE media_file_id=? AND face_attributes IS NOT NULL",
                    (media_id,)
                )
            for far in face_attr_rows:
                attrs = json.loads(far["face_attributes"])
                eyes = attrs.get("EyesOpen")
                if isinstance(eyes, dict) and eyes.get("Value") is False:
                    has_closed_eyes = 1
                    break
        except Exception:
            pass

        # ── Write quality signals back to media_files ────────────────────────
        # If any face was detected, use the MAXIMUM face-crop sharpness as the
        # blur signal instead of the full-image Laplacian.  This avoids false
        # positives on shallow depth-of-field shots where the subject is sharp
        # but the background / foreground bokeh drives the global score down.
        if faces:
            face_sharpnesses = [
                f.quality_sharpness for f in faces if f.quality_sharpness is not None
            ]
            if face_sharpnesses:
                best_face_sharpness = max(face_sharpnesses)
                from backend.ml.quality_checker import classify_blur
                is_blurry, long_exposure_flag = classify_blur(best_face_sharpness, exp_s)
                blur_score = round(best_face_sharpness, 2)

        async with get_db() as db:
            await db.execute("""
                UPDATE media_files
                SET blur_score=?, is_blurry=?, long_exposure=?, has_closed_eyes=?
                WHERE id=?
            """, (blur_score, is_blurry, long_exposure_flag, has_closed_eyes, media_id))

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
# Phase 3b: Auto-merge (high confidence) + notify borderline suggestions
# ---------------------------------------------------------------------------

async def _phase_auto_merge() -> None:
    """Compare named-person centroids to unnamed clusters.

    Thresholds (all configurable in settings):
    - sim >= auto_name_threshold (default 0.98): auto-assign name silently —
      very high confidence, near-identical centroid.
    - sim >= merge_suggest_threshold (default 0.63): pop a "Same person?"
      suggestion card for the user to confirm.

    Person centroids are stored in persons.centroid (persisted across photo
    deletions). On first run / after adding new faces to a person the centroid
    is recomputed from embeddings and stored so it survives future removals.
    """
    from backend.pipeline.centroid import update_person_centroid, load_centroid

    auto_name_threshold   = settings.auto_name_threshold
    suggest_threshold     = settings.merge_suggest_threshold

    logger.info(
        "Phase 3b: Auto-name check (name≥%.2f, suggest≥%.2f)",
        auto_name_threshold, suggest_threshold,
    )

    async with get_db() as db:
        persons = await db.execute_fetchall("""
            SELECT p.id AS person_id, p.name, p.centroid, p.centroid_n
            FROM persons p
            WHERE p.is_merged = 0 AND p.name IS NOT NULL
        """)
        if not persons:
            return

        clusters = await db.execute_fetchall("""
            SELECT c.id AS cluster_id, c.member_count,
                   MIN(f.id) AS representative_face_id
            FROM clusters c
            JOIN faces f ON f.cluster_id = c.id AND f.thumbnail_path IS NOT NULL
            WHERE c.person_id IS NULL
            GROUP BY c.id
        """)
        if not clusters:
            return

        rejected_rows = await db.execute_fetchall(
            "SELECT person_id, cluster_id FROM rejected_suggestions"
        )
        rejected = {(r["person_id"], r["cluster_id"]) for r in rejected_rows}

        # Build person centroids — use stored centroid when available, otherwise
        # derive from embeddings (first run / legacy rows) and persist it.
        person_data: list[dict] = []
        for p in persons:
            pid = p["person_id"]

            if p["centroid"]:
                # Use stored centroid (centroid_n=0 means all live photos were
                # removed but the vector is preserved for re-identification)
                centroid = load_centroid(p["centroid"])
            else:
                # Derive from embeddings and persist for future runs
                emb_rows = await db.execute_fetchall("""
                    SELECT e.vector FROM embeddings e
                    JOIN faces f ON f.id = e.face_id
                    WHERE f.person_id = ?
                """, (pid,))
                if not emb_rows:
                    continue
                vecs = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in emb_rows])
                centroid = vecs.mean(axis=0)
                norm = np.linalg.norm(centroid)
                if norm > 0:
                    centroid /= norm
                await db.execute(
                    "UPDATE persons SET centroid=?, centroid_n=? WHERE id=?",
                    (centroid.tobytes(), len(emb_rows), pid),
                )

            # Representative face thumbnail for the suggestion card
            rep_row = await (await db.execute("""
                SELECT f.id FROM faces f
                WHERE f.person_id = ? AND f.thumbnail_path IS NOT NULL
                LIMIT 1
            """, (pid,))).fetchone()
            rep_face_id = rep_row["id"] if rep_row else None

            person_data.append({
                "person_id": pid,
                "name":      p["name"],
                "centroid":  centroid,
                "face_id":   rep_face_id,
            })

        # Build cluster centroids (clusters are transient; no need to persist)
        cluster_data: list[dict] = []
        for c in clusters:
            emb_rows = await db.execute_fetchall("""
                SELECT e.vector FROM embeddings e
                JOIN faces f ON f.id = e.face_id
                WHERE f.cluster_id = ?
            """, (c["cluster_id"],))
            if not emb_rows:
                continue
            vecs = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in emb_rows])
            centroid = vecs.mean(axis=0)
            norm = np.linalg.norm(centroid)
            if norm > 0:
                centroid /= norm
            cluster_data.append({
                "cluster_id":   c["cluster_id"],
                "member_count": c["member_count"],
                "face_id":      c["representative_face_id"],
                "centroid":     centroid,
            })

    auto_named = 0
    suggestions: list[dict] = []

    # Already-named cluster IDs (prevent double-assignment)
    named_cluster_ids: set[int] = set()

    for p in person_data:
        pid   = p["person_id"]
        pname = p["name"]
        pc    = p["centroid"]
        for c in cluster_data:
            cid = c["cluster_id"]
            if cid in named_cluster_ids:
                continue
            if (pid, cid) in rejected:
                continue
            sim = float(np.dot(pc, c["centroid"]))
            if sim >= auto_name_threshold:
                async with get_db() as db:
                    await db.execute(
                        "UPDATE clusters SET person_id=? WHERE id=?", (pid, cid)
                    )
                    await db.execute(
                        "UPDATE faces SET person_id=? WHERE cluster_id=?", (pid, cid)
                    )
                    await db.execute("""
                        UPDATE persons
                        SET photo_count = (
                            SELECT COUNT(DISTINCT media_file_id) FROM faces WHERE person_id = ?
                        ) WHERE id = ?
                    """, (pid, pid))
                    await db.execute("""
                        INSERT OR REPLACE INTO writeback_queue (media_file_id)
                        SELECT DISTINCT media_file_id FROM faces WHERE cluster_id=?
                    """, (cid,))
                    # Refresh stored centroid to include the newly-assigned faces
                    await update_person_centroid(db, pid)
                named_cluster_ids.add(cid)
                auto_named += 1
                logger.info(
                    "Auto-named cluster %d → '%s' (sim=%.3f)", cid, pname, sim
                )
            elif sim >= suggest_threshold:
                suggestions.append({
                    "person_id":       pid,
                    "person_name":     pname,
                    "person_face_id":  p["face_id"],
                    "cluster_id":      cid,
                    "cluster_face_id": c["face_id"],
                    "similarity":      round(sim, 3),
                    "member_count":    c["member_count"],
                })

    if suggestions:
        # Deduplicate: keep highest-sim suggestion per cluster
        seen: dict[int, dict] = {}
        for s in suggestions:
            cid = s["cluster_id"]
            if cid not in seen or s["similarity"] > seen[cid]["similarity"]:
                seen[cid] = s
        await broadcast("merge_suggestions", suggestions=list(seen.values())[:10])

    logger.info(
        "Phase 3b: %d auto-named, %d suggestions surfaced", auto_named, len(suggestions)
    )


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
