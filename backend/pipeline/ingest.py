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
import unicodedata
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
async def run_reprocess(force_retag: bool = False) -> None:
    """
    Full reprocess of the existing library without a filesystem walk.

    Steps:
      1. Re-detect faces on photos not owned by a named person (respects
         updated detection settings; always-ignored faces are preserved).
      2. Re-cluster all unowned face embeddings with updated HDBSCAN settings.
      3. Re-run Phase 3b auto-merge + auto-suppress against ignored persons.
      4. Re-run Phase 3c VIP-history name restore for newly-detected faces.
      5. Re-run quality signals (blur, closed-eyes) on stored thumbnails.
      6. Rebuild analysis documents so all changes appear in the UI.
    """
    logger.info("=== Reprocess start (force_retag=%s) ===", force_retag)
    await broadcast("pipeline_start", folder="[library reprocess]")
    await settings_store.load_cache()
    _ensure_models()

    # When force_retag is requested, reset the tags_done flag on every
    # non-stub file so Phase 4 re-runs YOLO/CLIP inference on the full library.
    if force_retag:
        async with get_db() as db:
            await db.execute(
                "UPDATE media_files SET tags_done = 0 WHERE is_stub = 0"
            )
        logger.info("force_retag: tags_done reset for all files")
        await broadcast("phase_start", phase="force_retag_reset")

    # -------------------------------------------------------------------------
    # Step 1: Prepare files for face re-detection
    # -------------------------------------------------------------------------
    # For every media file where NO face belongs to a named (non-ignored)
    # person we:
    #   a) Delete unowned face rows and their embeddings
    #   b) Reset ingest_state → 'scanned' so _phase_embed re-processes them
    #
    # Files that DO have named-person faces are left untouched — those
    # assignments are preserved and will survive the re-cluster.
    #
    # Faces assigned to always-ignored persons (is_ignored=1) have
    # person_id IS NOT NULL, so the WHERE person_id IS NULL condition
    # below never touches them — they cannot re-surface.
    await broadcast("phase_start", phase="redetect_prep")
    async with get_db() as db:
        _cnt = await (await db.execute("""
            SELECT COUNT(DISTINCT m.id) AS n
            FROM media_files m
            WHERE m.is_stub = 0
              AND m.id NOT IN (
                  SELECT DISTINCT f.media_file_id FROM faces f
                  JOIN persons p ON p.id = f.person_id
                  WHERE p.name IS NOT NULL AND p.is_ignored = 0
              )
        """)).fetchone()
        redetect_count = _cnt["n"] if _cnt else 0

        # 1a. Drop embeddings for unowned faces in the re-detection scope
        await db.execute("""
            DELETE FROM embeddings
            WHERE face_id IN (
                SELECT f.id FROM faces f
                WHERE f.person_id IS NULL
                  AND f.media_file_id NOT IN (
                      SELECT DISTINCT f2.media_file_id FROM faces f2
                      JOIN persons p ON p.id = f2.person_id
                      WHERE p.name IS NOT NULL AND p.is_ignored = 0
                  )
            )
        """)

        # 1b. Drop the unowned face rows themselves
        await db.execute("""
            DELETE FROM faces
            WHERE person_id IS NULL
              AND media_file_id NOT IN (
                  SELECT DISTINCT f2.media_file_id FROM faces f2
                  JOIN persons p ON p.id = f2.person_id
                  WHERE p.name IS NOT NULL AND p.is_ignored = 0
              )
        """)

        # 1c. Reset ingest_state so _phase_embed re-processes these files
        await db.execute("""
            UPDATE media_files SET ingest_state = 'scanned'
            WHERE is_stub = 0
              AND id NOT IN (
                  SELECT DISTINCT f.media_file_id FROM faces f
                  JOIN persons p ON p.id = f.person_id
                  WHERE p.name IS NOT NULL AND p.is_ignored = 0
              )
        """)

    logger.info("Redetect prep: %d files reset for re-detection", redetect_count)
    await broadcast("phase_complete", phase="redetect_prep", count=redetect_count)

    # Step 2: Re-detect + embed on all 'scanned' files.
    # Files that still have embeddings (named-person files) are skipped by
    # the existing idempotency guard inside _phase_embed.
    await _phase_embed()

    # Step 3: Re-cluster all unowned face embeddings.
    # Named/ignored-person faces (cluster_id IS NOT NULL) are excluded.
    # This picks up both freshly-detected faces AND previously-stored
    # embeddings from mixed files (some named, some unnamed).
    await _phase_cluster()

    # Step 3a: Refresh co-occurrence graph from existing named persons so
    # the singleton-recovery boost has current data to query against.
    await _phase_build_cooccurrence()

    # Step 3b: Absorb HDBSCAN noise singletons into nearby clusters.
    # Uses co-occurrence data built above for confidence boost.
    await _phase_recover_singletons()

    # Step 4: Auto-merge + suppress.
    # Matches new clusters → named persons (auto-name or suggestion).
    # Matches new clusters → ignored persons (silently suppressed).
    await _phase_auto_merge()

    # Step 4b: Rebuild co-occurrence again to capture any new assignments
    # made by singleton recovery and auto-merge in this run.
    await _phase_build_cooccurrence()

    # Step 5: Restore VIP-history names for newly-detected faces in files
    # that VIP previously wrote XMP data to.
    await _phase_restore_vip_names()

    # Step 5b: If the user requested a full re-tag, run Phase 4 now.
    # (tags_done was already reset to 0 at the top of this function.)
    if force_retag:
        await _phase_tag()

    # Step 6: quality re-check on stored photo thumbnails
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

    # Step 7: Rebuild analysis documents so all changes appear in the UI
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

    await broadcast("pipeline_complete", folder="[library reprocess]")
    logger.info("=== Reprocess complete ===")


# ---------------------------------------------------------------------------
# Single-photo reprocess entry point
# ---------------------------------------------------------------------------
async def run_single_reprocess(media_id: int) -> None:
    """
    Re-detect faces in a single photo without scanning the full library.

    Steps:
      1. Delete unowned face embeddings and face rows for this photo.
         Named-person assignments are preserved.
      2. Reset this photo's ingest_state to 'scanned'.
      3. Re-run _phase_embed (with force bypass of the idempotency guard),
         _phase_cluster, _phase_auto_merge, and _phase_restore_vip_names.

    Useful when a face covering ≥30% of the frame was missed on the initial
    scan — triggering this avoids a full-library reprocess.
    """
    logger.info("=== Single-photo reprocess: media_id=%d ===", media_id)
    await broadcast("pipeline_start", folder=f"[reprocess photo {media_id}]")
    await settings_store.load_cache()
    _ensure_models()

    async with get_db() as db:
        row = await (
            await db.execute(
                "SELECT id, file_path, is_stub FROM media_files "
                "WHERE id=? AND removed_from_app=0",
                (media_id,),
            )
        ).fetchone()

    if not row:
        logger.error("Single reprocess: media_id=%d not found or removed", media_id)
        await broadcast("pipeline_complete", folder=f"[reprocess photo {media_id}]")
        return

    if row["is_stub"]:
        logger.warning(
            "Single reprocess: media_id=%d is an iCloud stub — skipping", media_id
        )
        await broadcast("pipeline_complete", folder=f"[reprocess photo {media_id}]")
        return

    async with get_db() as db:
        # Drop ALL embeddings for every face on this file (named and unnamed).
        # Keeping named-person face rows while force-re-detecting inserts a
        # second identical row for every already-named face, creating duplicates.
        # person centroids are persisted in the persons table so _phase_auto_merge
        # will re-assign names to the new clusters at high confidence (≥0.98).
        await db.execute("""
            DELETE FROM embeddings
            WHERE face_id IN (
                SELECT id FROM faces WHERE media_file_id = ?
            )
        """, (media_id,))
        # Drop ALL face rows for this file
        await db.execute(
            "DELETE FROM faces WHERE media_file_id = ?",
            (media_id,),
        )
        # Reset state so _phase_embed picks this file up
        await db.execute(
            "UPDATE media_files SET ingest_state = 'scanned' WHERE id = ?",
            (media_id,),
        )

    logger.info("Single reprocess: cleared all faces for media_id=%d", media_id)

    # No force_ids needed — all face rows were deleted so the idempotency
    # guard in _phase_embed sees no existing embeddings and processes normally.
    await _phase_embed()
    await _phase_cluster()
    await _phase_build_cooccurrence()
    await _phase_recover_singletons()
    await _phase_auto_merge()
    await _phase_build_cooccurrence()
    await _phase_restore_vip_names()

    await broadcast("pipeline_complete", folder=f"[reprocess photo {media_id}]")
    logger.info("=== Single-photo reprocess complete: media_id=%d ===", media_id)


# ---------------------------------------------------------------------------
# Batch reprocess entry point
# ---------------------------------------------------------------------------

async def run_batch_reprocess(media_ids: list[int]) -> None:
    """
    Re-detect faces in a batch of photos without scanning the full library.

    Runs the same steps as run_single_reprocess but batches the DB cleanup
    and runs embed → cluster → auto-merge → name-restore once across all
    selected photos, which is far more efficient than calling
    run_single_reprocess sequentially.

    Named-person assignments are restored via _phase_auto_merge, which
    compares new unnamed clusters against persisted person centroids.
    """
    if not media_ids:
        return

    label = f"[reprocess {len(media_ids)} photo{'s' if len(media_ids) != 1 else ''}]"
    logger.info("=== Batch reprocess start: %d photos ===", len(media_ids))
    await broadcast("pipeline_start", folder=label)
    await settings_store.load_cache()
    _ensure_models()

    placeholders = ",".join("?" * len(media_ids))

    async with get_db() as db:
        # Verify all requested IDs exist and are not stubs/removed
        rows = await (
            await db.execute(
                f"SELECT id, is_stub FROM media_files "
                f"WHERE id IN ({placeholders}) AND removed_from_app=0",
                media_ids,
            )
        ).fetchall()

    valid_ids = [r["id"] for r in rows if not r["is_stub"]]
    skipped = len(media_ids) - len(valid_ids)
    if skipped:
        logger.warning(
            "Batch reprocess: skipped %d media IDs (stubs or not found)", skipped
        )

    if not valid_ids:
        logger.error("Batch reprocess: no valid media IDs to process")
        await broadcast("pipeline_complete", folder=label)
        return

    placeholders = ",".join("?" * len(valid_ids))

    async with get_db() as db:
        # Drop ALL embeddings and face rows for selected files (named + unnamed).
        # Keeping named-face rows while force-re-detecting creates a second
        # identical row for every already-named face (duplicate person bug).
        # Named assignments are restored by _phase_auto_merge which compares
        # new unnamed clusters against persisted person centroids at ≥0.98 sim.
        await db.execute(f"""
            DELETE FROM embeddings
            WHERE face_id IN (
                SELECT id FROM faces
                WHERE media_file_id IN ({placeholders})
            )
        """, valid_ids)
        # Drop ALL face rows for the selected files
        await db.execute(
            f"DELETE FROM faces "
            f"WHERE media_file_id IN ({placeholders})",
            valid_ids,
        )
        # Reset state so _phase_embed picks these files up
        await db.execute(
            f"UPDATE media_files SET ingest_state = 'scanned' "
            f"WHERE id IN ({placeholders})",
            valid_ids,
        )

    logger.info(
        "Batch reprocess: cleared all faces for %d photos", len(valid_ids)
    )

    # No force_ids needed — all face rows were deleted so the idempotency
    # guard in _phase_embed sees no existing embeddings and processes normally.
    await _phase_embed()
    await _phase_cluster()
    await _phase_build_cooccurrence()
    await _phase_recover_singletons()
    await _phase_auto_merge()
    await _phase_build_cooccurrence()
    await _phase_restore_vip_names()

    await broadcast("pipeline_complete", folder=label)
    logger.info("=== Batch reprocess complete: %d photos ===", len(valid_ids))


# ---------------------------------------------------------------------------
# Model migration entry point
# ---------------------------------------------------------------------------
async def run_model_migration() -> None:
    """
    Re-embed all faces under the currently configured model and re-cluster.

    Must be triggered AFTER changing insightface_model in config.py and
    restarting the server so the new model files are loaded.

    Steps:
      1. Force-reload the detector with the current model config.
      2. Re-embed every named face from its saved 200×200 thumbnail JPEG
         using the new model.  Embeddings that cannot be re-embedded are
         dropped to prevent vector-space mixing.
      3. Recompute persisted centroids for all named persons.
      4. Clear all unnamed face embeddings, face rows, and clusters.
      5. Reset unnamed-only files to 'scanned' for fresh re-detection.
      6. Re-run embed → cluster → auto-merge → name-restore pipeline.

    Named person assignments are preserved throughout.
    Note: unnamed faces in files that also contain named faces will be
    absent from results until the next full folder scan (Run Scan).
    """
    from backend.pipeline.centroid import update_person_centroid

    logger.info("=== Model migration start ===")
    await broadcast("pipeline_start", folder="[model migration]")
    await settings_store.load_cache()

    # Force-reload the detector so it picks up the new insightface_model
    # from config — even if it was already loaded with the old model in
    # this process.
    global _models_loaded
    _models_loaded = False
    _detector._loaded_mode = None
    _ensure_models()

    # ------------------------------------------------------------------
    # Step 1: Re-embed named faces from saved thumbnails
    # ------------------------------------------------------------------
    await broadcast("phase_start", phase="migrate_named_faces")

    async with get_db() as db:
        named_face_rows = await db.execute_fetchall("""
            SELECT f.id AS face_id, f.thumbnail_path
            FROM faces f
            JOIN persons p ON p.id = f.person_id
            WHERE p.name IS NOT NULL AND p.is_ignored = 0
              AND EXISTS (SELECT 1 FROM embeddings e WHERE e.face_id = f.id)
        """)

    loop = asyncio.get_event_loop()
    remigrated = 0
    failed_ids: list[int] = []

    for row in named_face_rows:
        face_id   = row["face_id"]
        thumb_str = row["thumbnail_path"]

        if not thumb_str or not Path(thumb_str).exists():
            logger.warning(
                "Migration: no thumbnail for named face %d — embedding dropped", face_id
            )
            failed_ids.append(face_id)
            continue

        try:
            img_arr = await loop.run_in_executor(
                None,
                lambda p=thumb_str: np.array(_PILImage.open(p).convert("RGB")),
            )
        except Exception as exc:
            logger.warning(
                "Migration: cannot open thumbnail for face %d: %s — embedding dropped",
                face_id, exc,
            )
            failed_ids.append(face_id)
            continue

        vector = await loop.run_in_executor(None, _detector.embed_from_array, img_arr)

        if vector is None:
            logger.warning(
                "Migration: no embedding from thumbnail for face %d — embedding dropped",
                face_id,
            )
            failed_ids.append(face_id)
            continue

        async with get_db() as db:
            await db.execute(
                "UPDATE embeddings SET vector=?, model_version=? WHERE face_id=?",
                (_embedder.vector_to_bytes(vector), _embedder.model_version, face_id),
            )
        remigrated += 1

    # Delete embeddings that could not be re-embedded so no buffalo_l
    # vectors remain mixed in with the new model's vectors.
    if failed_ids:
        async with get_db() as db:
            ph = ",".join("?" * len(failed_ids))
            await db.execute(
                f"DELETE FROM embeddings WHERE face_id IN ({ph})", failed_ids
            )
        logger.warning(
            "Migration: dropped %d named-face embedding(s) that could not be re-embedded",
            len(failed_ids),
        )

    logger.info(
        "Migration step 1: %d named face(s) re-embedded, %d dropped",
        remigrated, len(failed_ids),
    )
    await broadcast("phase_complete", phase="migrate_named_faces", count=remigrated)

    # ------------------------------------------------------------------
    # Step 2: Recompute persisted centroids for all named persons
    # ------------------------------------------------------------------
    await broadcast("phase_start", phase="recompute_centroids")

    async with get_db() as db:
        named_persons = await db.execute_fetchall(
            "SELECT id FROM persons WHERE name IS NOT NULL AND is_ignored = 0 AND is_merged = 0"
        )
        for p in named_persons:
            await update_person_centroid(db, p["id"])

    logger.info(
        "Migration step 2: centroids recomputed for %d named person(s)", len(named_persons)
    )
    await broadcast("phase_complete", phase="recompute_centroids")

    # ------------------------------------------------------------------
    # Step 3: Clear all unnamed faces, their embeddings, and clusters
    #
    # Unlike run_reprocess(), we clear unnamed faces from ALL files
    # (including mixed files) so no old-model vectors remain in the
    # cluster pool.  Unnamed faces in mixed files will be re-detected
    # on the next full folder scan.
    # ------------------------------------------------------------------
    await broadcast("phase_start", phase="redetect_prep")

    async with get_db() as db:
        # 3a. Drop embeddings for every face with no named-person assignment
        await db.execute("""
            DELETE FROM embeddings
            WHERE face_id IN (
                SELECT f.id FROM faces f WHERE f.person_id IS NULL
            )
        """)

        # 3b. Drop the face rows themselves
        await db.execute("DELETE FROM faces WHERE person_id IS NULL")

        # 3c. Reset files that have no named faces → 'scanned' for re-detection
        await db.execute("""
            UPDATE media_files SET ingest_state = 'scanned'
            WHERE is_stub = 0
              AND id NOT IN (
                  SELECT DISTINCT f.media_file_id FROM faces f
                  JOIN persons p ON p.id = f.person_id
                  WHERE p.name IS NOT NULL AND p.is_ignored = 0
              )
        """)

    await broadcast("phase_complete", phase="redetect_prep")

    # ------------------------------------------------------------------
    # Step 4: Re-detect + embed → cluster → singleton-recovery → auto-merge → name-restore
    # ------------------------------------------------------------------
    await _phase_embed()
    await _phase_cluster()
    await _phase_build_cooccurrence()
    await _phase_recover_singletons()
    await _phase_auto_merge()
    await _phase_build_cooccurrence()
    await _phase_restore_vip_names()

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

    await broadcast("pipeline_complete", folder="[model migration]")
    logger.info("=== Model migration complete ===")


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

    # -- Phase 3a-i: Refresh co-occurrence so singleton recovery can use it --
    await _phase_build_cooccurrence()

    # -- Phase 3a-ii: Singleton recovery — FAISS nearest-neighbour pass ------
    await _phase_recover_singletons()

    # -- Phase 3b: Auto-merge high-conf + surface borderline suggestions ----
    await _phase_auto_merge()

    # -- Phase 3b-ii: Rebuild co-occurrence to capture this run's assignments -
    await _phase_build_cooccurrence()

    # -- Phase 3c: Restore person names from VIP History -------------------
    # When ExifTool has previously written named face regions to a photo,
    # the file hash changes.  On re-import the file is treated as new, so
    # faces are re-detected with no person assignments.  This phase reads
    # the external_exif snapshot (captured at first INSERT) and matches
    # the stored MWG face regions back to the freshly-detected face clusters
    # by bounding-box centre proximity, then re-assigns person names.
    await _phase_restore_vip_names()

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
# Phase 1 tuning — adjust to taste for your hardware / network
# ---------------------------------------------------------------------------
_SCAN_BATCH_SIZE  = 500   # fallback default (overridden by admin setting 'exif_batch_size')
_HASH_CONCURRENCY = 8     # simultaneous SHA-256 read streams
_TAG_BATCH_SIZE   = 16    # images per YOLO GPU forward pass in Phase 4


async def _phase_scan(folder: Path) -> None:
    logger.info("Phase 1: Scanning %s", folder)
    await broadcast("phase_start", phase="scan")

    # Drain the walker up front.  walk_folder already buffers the full
    # os.walk result in a thread-pool call, so this just consumes the async
    # generator without additional blocking.
    all_paths: list[Path] = []
    async for file_path in walk_folder(folder):
        all_paths.append(file_path)

    total_files = len(all_paths)
    if total_files == 0:
        await broadcast("phase_complete", phase="scan", scanned=0, skipped=0)
        logger.info("Phase 1 complete: 0 scanned, 0 skipped")
        return

    scan_batch_size = int(settings_store.get("exif_batch_size") or _SCAN_BATCH_SIZE)
    exif_timeout    = int(settings_store.get("exif_batch_timeout") or 300)
    logger.info(
        "Phase 1: %d files to process (batches of %d, timeout %ds)",
        total_files, scan_batch_size, exif_timeout,
    )

    scanned = 0
    skipped = 0
    loop = asyncio.get_event_loop()
    hash_sem = asyncio.Semaphore(_HASH_CONCURRENCY)
    exif_reader = ExifToolReader()

    for batch_start in range(0, total_files, scan_batch_size):
        batch = all_paths[batch_start : batch_start + scan_batch_size]

        # ── 1a + 1b: ExifTool (one subprocess for the whole batch) and ────────
        # file hashing (up to _HASH_CONCURRENCY concurrent readers) run in
        # parallel via asyncio.gather so network I/O and CPU overlap.
        async def _hash_one(path: Path) -> tuple[Path, str]:
            async with hash_sem:
                h = await loop.run_in_executor(None, compute_hash, path)
                return path, h

        gather_result = await asyncio.gather(
            exif_reader.read_batch(batch, timeout=exif_timeout),
            *[_hash_one(p) for p in batch],
        )
        exif_map: dict[str, dict] = gather_result[0]          # str(path) → meta
        path_to_hash: dict[str, str] = {str(p): h for p, h in gather_result[1:]}

        # ── 1c: One DB connection + one implicit transaction for the batch ─────
        # aiosqlite commits at __aexit__ — all inserts/updates for the batch
        # land in a single fsync instead of one fsync per file.
        async with get_db() as db:
            for file_path in batch:
                # NFC normalisation: prevents false "moved" events when the
                # same path is seen via HFS+ (NFD) on one scan and SMB (NFC)
                # on the next.
                file_path_str = unicodedata.normalize("NFC", str(file_path))
                file_hash = path_to_hash[str(file_path)]
                meta = exif_map.get(str(file_path)) or {}

                skip, existing_id = await check_idempotency(db, file_hash, file_path_str)
                if skip:
                    skipped += 1
                    continue

                stat = file_path.stat()
                _on_mounted_volume = file_path_str.startswith("/Volumes/")
                is_stub = (not _on_mounted_volume) and (stat.st_size < settings.stub_max_size_bytes)

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
                        file_path_str, stat.st_size, meta.get("file_format"),
                        meta.get("camera_make"), meta.get("camera_model"),
                        meta.get("date_taken"), meta.get("gps_lat"), meta.get("gps_lon"),
                        meta.get("width"), meta.get("height"), int(is_stub),
                        meta.get("exposure_time_s"), existing_id,
                    ))
                else:
                    # New file — reuse the file's own XMP:Identifier as vip_id
                    # only if it hasn't already been claimed by a different row
                    # (RAW+JPEG pairs, camera firmware duplication, or files
                    # shared across folders can all carry the same identifier).
                    # In those cases fall back to a fresh UUID.
                    _candidate_id = meta.get("xmp_identifier")
                    if _candidate_id:
                        _taken = await (await db.execute(
                            "SELECT 1 FROM media_files WHERE vip_id=?", (_candidate_id,)
                        )).fetchone()
                        new_vip_id = _candidate_id if _taken is None else str(uuid.uuid4())
                    else:
                        new_vip_id = str(uuid.uuid4())

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
                        file_path_str, file_hash, stat.st_size, meta.get("file_format"),
                        meta.get("camera_make"), meta.get("camera_model"),
                        meta.get("date_taken"), meta.get("gps_lat"), meta.get("gps_lon"),
                        meta.get("width"), meta.get("height"), int(is_stub),
                        meta.get("exposure_time_s"),
                        external_exif_json,
                    ))

                scanned += 1

        # Progress update and event-loop yield after each batch
        await broadcast("scan_progress", done=scanned + skipped, total=total_files)
        await asyncio.sleep(0)

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


async def _phase_embed(force_ids: set[int] | None = None) -> None:
    """
    Detect and embed faces in all 'scanned' files.

    force_ids: when provided, bypass the idempotency skip-guard for these
    specific media IDs.  Used by run_single_reprocess so that a file whose
    named-person embeddings were deliberately kept can be fully re-detected.
    """
    logger.info("Phase 2: Embedding faces (force_ids=%s)", force_ids)
    await broadcast("phase_start", phase="embed")

    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id, file_path FROM media_files WHERE ingest_state='scanned' AND is_stub=0"
        )

    total = len(rows)
    logger.info("%d files to embed", total)

    # Read concurrency limit from user-adjustable setting.
    # Default 4 is conservative — safe on a MacBook; raise in Admin → System.
    concurrency = int(settings_store.get("embed_concurrency") or 4)
    logger.info("Phase 2: concurrency=%d", concurrency)
    sem = asyncio.Semaphore(concurrency)

    # Shared mutable counter across concurrent workers (asyncio is single-
    # threaded so plain int inside a list is safe without a lock).
    _counter = [0]  # [processed]

    async def _process_one(row: aiosqlite.Row) -> None:
        async with sem:
            media_id = row["id"]
            file_path = row["file_path"]
            path = Path(file_path)

            if not path.exists():
                logger.warning("File not on disk (possibly offloaded): %s", file_path)
                return

            # Extract embedded JPEG preview
            preview_path = await extract_preview(path)
            if preview_path is None:
                async with get_db() as db:
                    await db.execute(
                        "UPDATE media_files SET ingest_state='embedded' WHERE id=?", (media_id,)
                    )
                _counter[0] += 1
                return

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
            if _emb_check and (force_ids is None or media_id not in force_ids):
                async with get_db() as db:
                    await db.execute(
                        "UPDATE media_files SET ingest_state='embedded' WHERE id=?", (media_id,)
                    )
                _counter[0] += 1
                return

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
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, _make_photo_thumb, preview_path, media_id
            )
            faces = await loop.run_in_executor(
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

            _counter[0] += 1
            if _counter[0] % 10 == 0:
                await broadcast("embed_progress", done=_counter[0], total=total)

    # Fan out all rows concurrently, bounded by the semaphore.
    await asyncio.gather(*[_process_one(row) for row in rows])

    await broadcast("phase_complete", phase="embed", processed=_counter[0])
    logger.info("Phase 2 complete: %d files processed", _counter[0])


# ---------------------------------------------------------------------------
# Phase 3: Cluster all embeddings
# ---------------------------------------------------------------------------
async def _phase_cluster() -> None:
    logger.info("Phase 3: Clustering")
    await broadcast("phase_start", phase="cluster")

    # Step 1: Clear any clusters that no longer have a named person.
    # Named-person clusters (person_id IS NOT NULL) are kept so assignments survive.
    async with get_db() as db:
        unnamed = await db.execute_fetchall("""
            SELECT c.id FROM clusters c
            WHERE NOT EXISTS (
                SELECT 1 FROM faces f
                JOIN persons p ON p.id = f.person_id
                WHERE f.cluster_id = c.id AND p.name IS NOT NULL AND p.is_ignored = 0
            )
        """)
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
        all_face_updates: list[tuple] = []   # (cluster_id, face_id) pairs
        all_cluster_ids:  list[int]   = []

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
            all_cluster_ids.append(cluster_id)
            all_face_updates.extend((cluster_id, fid) for fid in cluster.face_ids)

        # Batch all face cluster assignments in one executemany call
        # instead of one UPDATE per face (N → 1 round-trips).
        if all_face_updates:
            await db.executemany(
                "UPDATE faces SET cluster_id=? WHERE id=?", all_face_updates
            )

        # Advance media_files state for every newly-clustered file in one
        # query — never demote files already tagged by Phase 4.
        if all_cluster_ids:
            ph = ",".join("?" * len(all_cluster_ids))
            await db.execute(f"""
                UPDATE media_files SET ingest_state='clustered'
                WHERE id IN (
                    SELECT media_file_id FROM faces WHERE cluster_id IN ({ph})
                )
                  AND ingest_state NOT IN ('tagged')
            """, all_cluster_ids)

    # Rebuild FAISS index
    _faiss.build(face_ids, vectors)
    _faiss.save()

    await broadcast("phase_complete", phase="cluster", clusters=len(results))
    logger.info("Phase 3 complete: %d clusters", len(results))


# ---------------------------------------------------------------------------
# Phase 3a: Singleton recovery — FAISS nearest-neighbour pass
# ---------------------------------------------------------------------------

async def _phase_recover_singletons() -> None:
    """Absorb HDBSCAN noise singletons into existing clusters using FAISS.

    Problem: HDBSCAN labels low-density faces as noise (label=-1).  VIP
    promotes each noise face to its own singleton cluster so it appears in
    the UI.  The same person may appear twice in the same album yet end up
    as a real cluster (5 photos) + 2 singletons (bad angle, dark photo).

    Fix: After clustering, query FAISS with each singleton's centroid and
    find the k nearest face embeddings.  If the closest match belongs to
    an existing multi-face cluster (or a named person), absorb the singleton:

    - similarity >= singleton_auto_merge  → silent merge into cluster/person
    - similarity >= merge_suggest_threshold → surface as a suggestion card

    Only singletons are considered as the query side; any non-singleton can
    already appear as a neighbor.  Named-person clusters are valid targets
    (a singleton may be a known person photographed in an unusual situation).

    The FAISS index built by _phase_cluster() is reused in memory — no
    rebuild needed.  This phase is O(S × k) in FAISS lookups where S is the
    number of singletons and k is small (default 20).
    """
    from backend.database.settings_store import get as get_setting
    from backend.pipeline.centroid import update_person_centroid, load_centroid

    if _faiss.total == 0:
        logger.info("Phase 3a: FAISS index empty — skipping singleton recovery")
        return

    auto_th    = float(get_setting("auto_name_threshold"))   # default 0.98
    suggest_th = float(get_setting("merge_suggest_threshold"))  # default 0.63

    # Singleton recovery uses a slightly lower auto-threshold than person
    # auto-naming.  Two near-identical embeddings that both ended up as
    # HDBSCAN singletons almost certainly belong together.
    singleton_auto_th = max(suggest_th, min(auto_th, 0.88))

    logger.info(
        "Phase 3a: Singleton recovery (auto≥%.2f, suggest≥%.2f)",
        singleton_auto_th, suggest_th,
    )
    await broadcast("phase_start", phase="singleton_recovery")

    # -- Load singleton clusters (member_count == 1, person_id IS NULL) -----
    async with get_db() as db:
        singleton_rows = await db.execute_fetchall("""
            SELECT c.id AS cluster_id, c.centroid,
                   MIN(f.id) AS face_id
            FROM clusters c
            JOIN faces f ON f.cluster_id = c.id
            WHERE c.member_count = 1
              AND c.person_id IS NULL
            GROUP BY c.id
        """)

        # Build face_id → cluster_id map for all non-singleton unnamed clusters
        # (needed to find which cluster a FAISS hit belongs to)
        face_to_cluster_rows = await db.execute_fetchall("""
            SELECT f.id AS face_id, f.cluster_id, c.member_count,
                   f.person_id,
                   c.person_id AS cluster_person_id
            FROM faces f
            JOIN clusters c ON c.id = f.cluster_id
            WHERE f.cluster_id IS NOT NULL
        """)

    face_to_cluster: dict[int, dict] = {
        r["face_id"]: {
            "cluster_id":        r["cluster_id"],
            "member_count":      r["member_count"],
            "person_id":         r["cluster_person_id"],
        }
        for r in face_to_cluster_rows
    }

    if not singleton_rows:
        logger.info("Phase 3a: No singleton clusters to recover")
        await broadcast("phase_complete", phase="singleton_recovery", merged=0)
        return

    auto_merged   = 0
    suggestions: list[dict] = []
    # Track clusters we've already absorbed a singleton into this run
    # to prevent the same target being suggested multiple times in one pass
    already_targeted: set[int] = set()

    for row in singleton_rows:
        singleton_cluster_id = row["cluster_id"]
        if not row["centroid"]:
            continue
        centroid = load_centroid(row["centroid"])

        # Query FAISS — exclude the singleton's own face from hits (it's in
        # the index too; subtract 1 from k so we always get real neighbours)
        hits = _faiss.search(centroid, k=21, threshold=suggest_th)
        # Filter out the singleton's own face
        own_face_id = row["face_id"]
        hits = [(fid, sim) for fid, sim in hits if fid != own_face_id]

        if not hits:
            continue

        best_face_id, best_sim = hits[0]
        target = face_to_cluster.get(best_face_id)
        if target is None:
            continue

        target_cluster_id = target["cluster_id"]
        target_person_id  = target["person_id"]

        if target_cluster_id == singleton_cluster_id:
            continue   # hit itself somehow

        # ── Social-context boost ───────────────────────────────────────────
        # If the singleton's photo also contains companions who have
        # previously appeared alongside the proposed target_person_id, we
        # credit extra confidence (up to +0.10).  This mirrors the temporal
        # co-occurrence signal Apple Photos uses to disambiguate look-alikes.
        # Only applicable when the target cluster already belongs to a known
        # named person; anonymous clusters don't have historical co-occurrence.
        boost = 0.0
        if target_person_id is not None:
            async with get_db() as _bdb:
                _br = await (await _bdb.execute("""
                    SELECT COUNT(DISTINCT companion.person_id) AS colocated
                    FROM faces s
                    JOIN faces companion
                      ON  companion.media_file_id = s.media_file_id
                      AND companion.person_id IS NOT NULL
                    WHERE s.cluster_id = ?
                      AND EXISTS (
                          SELECT 1 FROM faces f1
                          JOIN faces f2
                            ON  f2.media_file_id = f1.media_file_id
                            AND f2.person_id = ?
                          WHERE f1.person_id = companion.person_id
                      )
                """, (singleton_cluster_id, target_person_id))).fetchone()
            colocated = _br["colocated"] if _br else 0
            boost = min(0.10, colocated * 0.05)

        effective_sim = min(1.0, best_sim + boost)

        if effective_sim >= singleton_auto_th:
            # ── Silent auto-merge: move singleton faces into target cluster ─
            if target_cluster_id in already_targeted:
                continue
            async with get_db() as db:
                # Move the singleton's face(s) into the target cluster
                await db.execute(
                    "UPDATE faces SET cluster_id=?, person_id=? WHERE cluster_id=?",
                    (target_cluster_id, target_person_id, singleton_cluster_id),
                )
                # Delete the now-empty singleton cluster row
                await db.execute(
                    "DELETE FROM clusters WHERE id=?", (singleton_cluster_id,)
                )
                # Update target cluster member count
                await db.execute("""
                    UPDATE clusters SET member_count = (
                        SELECT COUNT(*) FROM faces WHERE cluster_id = ?
                    ) WHERE id = ?
                """, (target_cluster_id, target_cluster_id))
                # Also update centroid of the target cluster by recomputing
                # from its embeddings (now includes the absorbed singleton)
                emb_rows = await db.execute_fetchall("""
                    SELECT e.vector FROM embeddings e
                    JOIN faces f ON f.id = e.face_id
                    WHERE f.cluster_id = ?
                """, (target_cluster_id,))
                if emb_rows:
                    vecs = np.stack([
                        np.frombuffer(r["vector"], dtype=np.float32)
                        for r in emb_rows
                    ])
                    new_centroid = vecs.mean(axis=0)
                    norm = np.linalg.norm(new_centroid)
                    if norm > 0:
                        new_centroid /= norm
                    await db.execute(
                        "UPDATE clusters SET centroid=? WHERE id=?",
                        (new_centroid.tobytes(), target_cluster_id),
                    )
                # If the target cluster belongs to a named person, refresh
                # the person's centroid and queue photos for writeback.
                if target_person_id is not None:
                    await update_person_centroid(db, target_person_id)
                    await db.execute("""
                        INSERT OR REPLACE INTO writeback_queue (media_file_id)
                        SELECT DISTINCT media_file_id FROM faces WHERE cluster_id=?
                    """, (target_cluster_id,))

            already_targeted.add(target_cluster_id)
            auto_merged += 1
            logger.debug(
                "Singleton %d absorbed into cluster %d (sim=%.3f boost=%.2f eff=%.3f)",
                singleton_cluster_id, target_cluster_id, best_sim, boost, effective_sim,
            )

        else:
            # ── Suggestion (between suggest_th and singleton_auto_th) ───────
            # Only surface if the raw sim clears the suggest_th (effective_sim
            # may push borderline cases through to auto-merge above).
            if best_sim < suggest_th:
                continue
            # Look up the representative thumbnail for the target cluster
            async with get_db() as db:
                rep = await (await db.execute("""
                    SELECT MIN(f.thumbnail_path) AS thumb, c.member_count
                    FROM clusters c
                    JOIN faces f ON f.cluster_id = c.id AND f.thumbnail_path IS NOT NULL
                    WHERE c.id = ?
                    GROUP BY c.id
                """, (target_cluster_id,))).fetchone()
                s_rep = await (await db.execute("""
                    SELECT MIN(f.thumbnail_path) AS thumb
                    FROM faces f WHERE f.cluster_id = ?
                """, (singleton_cluster_id,))).fetchone()

            suggestions.append({
                "singleton_cluster_id": singleton_cluster_id,
                "singleton_thumb": s_rep["thumb"] if s_rep else None,
                "target_cluster_id": target_cluster_id,
                "target_person_id":  target_person_id,
                "target_member_count": rep["member_count"] if rep else 1,
                "target_thumb": rep["thumb"] if rep else None,
                "similarity": round(best_sim, 3),
            })

    # Broadcast a summary so the frontend can refresh the People tab
    await broadcast(
        "phase_complete",
        phase="singleton_recovery",
        merged=auto_merged,
        suggestions=len(suggestions),
    )
    logger.info(
        "Phase 3a: %d singletons absorbed, %d suggestions",
        auto_merged, len(suggestions),
    )


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
    from backend.database.settings_store import get as get_setting

    auto_name_threshold   = get_setting("auto_name_threshold")
    suggest_threshold     = get_setting("merge_suggest_threshold")

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
                logger.debug(
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

    # ── Silently suppress new clusters that match ignored persons ────────────
    # Ignored persons have no name so they are excluded from the person_data
    # loop above.  We compare their stored centroids against remaining unnamed
    # clusters here and auto-assign matches so they never surface in the UI.
    async with get_db() as db:
        ignored_persons = await db.execute_fetchall("""
            SELECT p.id AS person_id, p.centroid
            FROM persons p
            WHERE p.is_merged = 0 AND p.is_ignored = 1 AND p.centroid IS NOT NULL
        """)

    for ip in ignored_persons:
        ic = load_centroid(ip["centroid"])
        if ic is None:
            continue
        for c in cluster_data:
            cid = c["cluster_id"]
            if cid in named_cluster_ids:
                continue
            sim = float(np.dot(ic, c["centroid"]))
            if sim >= auto_name_threshold:
                async with get_db() as db:
                    await db.execute(
                        "UPDATE clusters SET person_id=? WHERE id=?",
                        (ip["person_id"], cid),
                    )
                    await db.execute(
                        "UPDATE faces SET person_id=? WHERE cluster_id=?",
                        (ip["person_id"], cid),
                    )
                    await update_person_centroid(db, ip["person_id"])
                named_cluster_ids.add(cid)
                logger.debug(
                    "Auto-suppressed cluster %d → ignored person %d (sim=%.3f)",
                    cid, ip["person_id"], sim,
                )

    logger.info(
        "Phase 3b: %d auto-named, %d suggestions surfaced", auto_named, len(suggestions)
    )


# ---------------------------------------------------------------------------
# Phase 3b-ii: Rebuild person co-occurrence graph
# ---------------------------------------------------------------------------

async def _phase_build_cooccurrence() -> None:
    """
    Rebuild the person_cooccurrence table from the current face assignments.

    Each row = (person_a_id, person_b_id, count, last_seen_at) where:
      - person_a_id < person_b_id   (canonical ordering, no duplicate pairs)
      - count = number of distinct photos both persons appear in together
      - last_seen_at = MAX(date_taken) of shared photos (falls back to now())

    Strategy: full recompute rather than incremental, so the table is always
    consistent with the current face→person assignment even after merges or
    manual corrections.  At typical library sizes (<100 K photos) the query
    runs in <1 s with the indexes on faces.person_id and
    media_files.date_taken.
    """
    logger.info("Phase 3b-ii: rebuilding person co-occurrence graph …")
    async with get_db() as db:
        # Wipe stale edges — we rebuild fully every time for consistency.
        await db.execute("DELETE FROM person_cooccurrence")

        # Self-join faces on same media_file to find co-occurring persons.
        # CTE pre-computes canonical (person_a_id, person_b_id) ordering with
        # CASE so that the outer GROUP BY uses plain columns — SQLite does not
        # allow aggregate functions inside GROUP BY clauses.
        await db.execute("""
            INSERT INTO person_cooccurrence
                (person_a_id, person_b_id, count, last_seen_at)
            SELECT
                pairs.pa,
                pairs.pb,
                COUNT(DISTINCT pairs.media_file_id) AS count,
                COALESCE(MAX(pairs.date_taken), datetime('now')) AS last_seen_at
            FROM (
                SELECT
                    CASE WHEN f1.person_id < f2.person_id
                         THEN f1.person_id ELSE f2.person_id END AS pa,
                    CASE WHEN f1.person_id < f2.person_id
                         THEN f2.person_id ELSE f1.person_id END AS pb,
                    f1.media_file_id,
                    m.date_taken
                FROM faces f1
                JOIN faces f2
                  ON  f2.media_file_id = f1.media_file_id
                  AND f2.person_id     != f1.person_id
                  AND f2.person_id     IS NOT NULL
                JOIN media_files m ON m.id = f1.media_file_id
                WHERE f1.person_id IS NOT NULL
            ) AS pairs
            JOIN persons pa ON pa.id = pairs.pa
                           AND pa.is_merged = 0 AND pa.is_ignored = 0
            JOIN persons pb ON pb.id = pairs.pb
                           AND pb.is_merged = 0 AND pb.is_ignored = 0
            GROUP BY pairs.pa, pairs.pb
            HAVING COUNT(DISTINCT pairs.media_file_id) >= 1
        """)

        row = await (await db.execute("SELECT COUNT(*) AS n FROM person_cooccurrence")).fetchone()
        edge_count = row["n"] if row else 0

    logger.info("Phase 3b-ii: %d co-occurrence edges written", edge_count)
    await broadcast("phase_complete", phase="cooccurrence", edges=edge_count)


# ---------------------------------------------------------------------------
# Phase 3c: Restore VIP person names from pre-import history
# ---------------------------------------------------------------------------
async def _phase_restore_vip_names() -> None:
    """
    Re-assign person names that VIP previously wrote to photo files.

    When ExifTool writes metadata to a file the SHA-256 hash changes, so the
    next import treats it as a new file.  Faces are re-detected with no
    person assignments even though the history is captured in external_exif.

    This phase:
      1. Finds newly-ingested files whose external_exif snapshot has an
         "identifier" key (= VIP previously wrote to this file) and named
         face regions (XMP-mwg-rs:Regions with Name+bbox).
      2. Matches each named region to the closest newly-detected face using
         normalised bounding-box centre Euclidean distance.
      3. Finds or creates the named person record and assigns the face's
         cluster to that person.

    Only faces in the current pipeline run are affected (ingest_state IN
    ('embedded','clustered')).  Already-named faces are left alone.
    """
    import math

    logger.info("Phase 3c: Restoring VIP person names from history")
    from backend.pipeline.centroid import update_person_centroid

    async with get_db() as db:
        # Files ingested this run that have a VIP History snapshot
        rows = await db.execute_fetchall("""
            SELECT mf.id AS media_id, mf.external_exif
            FROM media_files mf
            WHERE mf.external_exif IS NOT NULL
              AND mf.external_exif LIKE '%"identifier"%'
              AND mf.ingest_state IN ('embedded', 'clustered')
        """)

    restored = 0

    for row in rows:
        media_id = row["media_id"]
        ext: dict = {}
        try:
            ext = json.loads(row["external_exif"])
        except Exception:
            continue

        # Must be a genuine VIP History file (identifier present)
        if not ext.get("identifier"):
            continue

        # --- Extract named regions from MWG RegionInfo ----------------------
        # Stored as raw ExifTool output: {RegionList: [{Name, Type, Area:{X,Y,W,H}}]}
        region_info = ext.get("region_info")
        named_regions: list[dict] = []   # {name, cx, cy}
        if isinstance(region_info, dict):
            for r in region_info.get("RegionList", []):
                name = r.get("Name", "").strip()
                area = r.get("Area", {})
                if name and isinstance(area, dict) and area.get("Unit") == "normalized":
                    # MWG Area.X / Area.Y are the CENTRE of the region
                    named_regions.append({
                        "name": name,
                        "cx":   float(area.get("X", 0)),
                        "cy":   float(area.get("Y", 0)),
                    })

        # Fallback: if no bbox regions but persons list has exactly one name
        # and the photo has exactly one detected face → safe to auto-assign.
        if not named_regions:
            persons_list: list[str] = ext.get("persons") or []
            if len(persons_list) == 1:
                async with get_db() as db:
                    face_count = await (await db.execute(
                        "SELECT COUNT(*) AS c FROM faces WHERE media_file_id=?",
                        (media_id,)
                    )).fetchone()
                if face_count and face_count["c"] == 1:
                    named_regions = [{"name": persons_list[0], "cx": None, "cy": None}]

        if not named_regions:
            continue

        # --- Get detected faces for this file --------------------------------
        async with get_db() as db:
            face_rows = await db.execute_fetchall("""
                SELECT f.id AS face_id, f.cluster_id,
                       f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,
                       f.person_id
                FROM faces f
                WHERE f.media_file_id = ?
            """, (media_id,))

        if not face_rows:
            continue

        # --- Match each named region to the closest face by centre distance --
        # bbox_x/y/w/h in DB are normalised top-left + width/height.
        # MWG regions use normalised centre X/Y.
        matched: list[tuple[str, int, int | None]] = []   # (name, face_id, cluster_id)

        unmatched_faces = list(face_rows)   # faces not yet claimed

        for region in named_regions:
            if region["cx"] is None:
                # Single-face single-name fallback — take the only face
                f = unmatched_faces[0]
                if f["person_id"] is None:  # don't overwrite existing name
                    matched.append((region["name"], f["face_id"], f["cluster_id"]))
                continue

            best_face = None
            best_dist = float("inf")
            for f in unmatched_faces:
                if f["person_id"] is not None:  # already named — skip
                    continue
                if f["bbox_x"] is None:
                    continue
                # Convert stored top-left bbox to centre
                fcx = f["bbox_x"] + f["bbox_w"] / 2
                fcy = f["bbox_y"] + f["bbox_h"] / 2
                dist = math.sqrt(
                    (fcx - region["cx"]) ** 2 +
                    (fcy - region["cy"]) ** 2
                )
                if dist < best_dist:
                    best_dist = dist
                    best_face = f

            # Accept match if the centres are within 15% of image dimensions
            if best_face is not None and best_dist <= 0.15:
                matched.append((region["name"], best_face["face_id"], best_face["cluster_id"]))
                unmatched_faces = [f for f in unmatched_faces if f["face_id"] != best_face["face_id"]]

        if not matched:
            continue

        # --- Assign person records -------------------------------------------
        async with get_db() as db:
            for person_name, face_id, cluster_id in matched:
                # Find existing named person or create one
                existing_person = await (await db.execute(
                    "SELECT id FROM persons WHERE name=? AND is_merged=0 LIMIT 1",
                    (person_name,)
                )).fetchone()

                if existing_person:
                    person_id = existing_person["id"]
                else:
                    import uuid as _uuid
                    cursor = await db.execute("""
                        INSERT INTO persons (uuid, name, named_at)
                        VALUES (?, ?, datetime('now'))
                    """, (str(_uuid.uuid4()), person_name))
                    person_id = cursor.lastrowid
                    logger.debug(
                        "Phase 3c: Created person '%s' (id=%d) from VIP History",
                        person_name, person_id
                    )

                # Assign face → person
                await db.execute(
                    "UPDATE faces SET person_id=? WHERE id=?",
                    (person_id, face_id)
                )

                # Assign cluster → person (if face belongs to a cluster)
                if cluster_id is not None:
                    await db.execute(
                        "UPDATE clusters SET person_id=? WHERE id=?",
                        (person_id, cluster_id)
                    )
                    # Assign all other faces in the same cluster too
                    await db.execute(
                        "UPDATE faces SET person_id=? WHERE cluster_id=? AND person_id IS NULL",
                        (person_id, cluster_id)
                    )

                # Queue for writeback so names stay written after this run
                await db.execute("""
                    INSERT OR REPLACE INTO writeback_queue (media_file_id)
                    VALUES (?)
                """, (media_id,))

                logger.debug(
                    "Phase 3c: Restored '%s' → face_id=%d, cluster_id=%s (media_id=%d)",
                    person_name, face_id, cluster_id, media_id
                )
                restored += 1

            # Persist centroid for all persons touched in this file
            touched_person_ids: set[int] = set()
            for _, _, cluster_id in matched:
                if cluster_id is not None:
                    pid_row = await (await db.execute(
                        "SELECT person_id FROM clusters WHERE id=?", (cluster_id,)
                    )).fetchone()
                    if pid_row and pid_row["person_id"]:
                        touched_person_ids.add(pid_row["person_id"])
            for pid in touched_person_ids:
                await update_person_centroid(db, pid)

    logger.info("Phase 3c complete: %d face name(s) restored from VIP History", restored)


# ---------------------------------------------------------------------------
# Phase 4: Tag — objects, animals, geography, places
# ---------------------------------------------------------------------------
async def _phase_tag() -> None:
    concurrency = int(settings_store.get("tag_concurrency"))
    logger.info(
        "Phase 4: Tagging (objects, animals, geography, places) "
        "[concurrency=%d, yolo_batch=%d]",
        concurrency, _TAG_BATCH_SIZE,
    )
    await broadcast("phase_start", phase="tag")

    # Load tagging models lazily (first pipeline run only)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _tagger.load)

    # Files that have not been tagged yet (tags_done = 0) and are at least
    # in the embedded/clustered/tagged state (i.e. past Phase 2).
    # This skips files whose content has not changed since the last scan,
    # avoiding redundant YOLO/CLIP inference on large libraries.
    # Use force_retag (via Rescan All) to re-run on the full library.
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT id, file_path, gps_lat, gps_lon
            FROM media_files
            WHERE tags_done = 0
              AND ingest_state IN ('embedded', 'clustered', 'tagged')
              AND is_stub = 0
        """)

    total = len(rows)
    logger.info("Phase 4: %d files to tag", total)
    if total == 0:
        await broadcast("phase_complete", phase="tag", tagged=0)
        return

    sem = asyncio.Semaphore(concurrency)
    _counter = [0]

    async def _process_batch(batch: list) -> None:
        async with sem:
            # Ensure previews exist for every image in the batch.
            preview_paths: list[Path] = []
            for row in batch:
                path = Path(row["file_path"])
                pp = settings.preview_dir / f"{path.stem}_{_hash_path(path)}.jpg"
                if not pp.exists():
                    pp = await extract_preview(path) or pp
                preview_paths.append(pp)

            gps_data  = [(row["gps_lat"], row["gps_lon"]) for row in batch]
            media_ids = [row["id"] for row in batch]

            # One YOLO GPU forward pass for up to _TAG_BATCH_SIZE images,
            # followed by per-image scene / landmark / geo models.
            items = [(pp, gps[0], gps[1]) for pp, gps in zip(preview_paths, gps_data)]
            tag_results = await loop.run_in_executor(None, _tagger.tag_batch, items)

            # Batch-persist all tags and state transitions in one DB write.
            async with get_db() as db:
                # Clear stale place tags before re-inserting so that a
                # force_retag run doesn't leave old Nominatim labels alongside
                # new MapKit-resolved labels (UNIQUE is on (file, category,
                # label) so differing strings would both survive INSERT OR IGNORE).
                if media_ids:
                    placeholders = ",".join("?" * len(media_ids))
                    await db.execute(
                        f"DELETE FROM media_tags WHERE category='place' "
                        f"AND media_file_id IN ({placeholders})",
                        media_ids,
                    )

                # Clear stale explicit tags before re-inserting (same reason as place tags).
                if media_ids:
                    placeholders = ",".join("?" * len(media_ids))
                    await db.execute(
                        f"DELETE FROM media_tags WHERE category='explicit' "
                        f"AND media_file_id IN ({placeholders})",
                        media_ids,
                    )

                tag_rows: list[tuple] = []
                gps_resolved_ids: list[int] = []   # files that got a GPS-resolved place label
                for media_id, result, (gps_lat, gps_lon) in zip(
                    media_ids, tag_results, gps_data
                ):
                    for label in result.objects:
                        tag_rows.append((media_id, "object", label, None, "yolov11"))
                    for label in result.animals:
                        tag_rows.append((media_id, "animal", label, None, "bioclip"))
                    for label in result.geography:
                        tag_rows.append((media_id, "geography", label, None, "places365"))
                    for label in result.places:
                        # First place label is the GPS-derived one when geo_source is set.
                        # Use the actual resolver name (mapkit/nominatim) so we can
                        # track source provenance in the DB; fall back to "clip".
                        is_geo_place = result.geo_source is not None and label == result.places[0]
                        model = result.geo_source if is_geo_place else "clip"
                        tag_rows.append((media_id, "place", label, None, model))
                        if is_geo_place:
                            gps_resolved_ids.append(media_id)
                    for label in result.explicit_labels:
                        tag_rows.append((media_id, "explicit", label, None, "nudenet"))

                if tag_rows:
                    await db.executemany("""
                        INSERT OR IGNORE INTO media_tags
                            (media_file_id, category, label, confidence, model)
                        VALUES (?,?,?,?,?)
                    """, tag_rows)

                # Queue files that received a GPS-resolved place label for
                # writeback so the location name reaches EXIF/XMP automatically.
                if gps_resolved_ids:
                    await db.executemany(
                        "INSERT OR REPLACE INTO writeback_queue (media_file_id) VALUES (?)",
                        [(mid,) for mid in gps_resolved_ids],
                    )

                # Mark tagging complete and advance ingest_state.
                # tags_done = 1 causes this file to be skipped on future
                # pipeline runs (unless force_retag resets it).
                await db.executemany(
                    "UPDATE media_files SET ingest_state='tagged', tags_done=1 WHERE id=?",
                    [(mid,) for mid in media_ids],
                )

            # Clean up previews now that both Phase 2 and Phase 4 are done.
            for pp in preview_paths:
                if pp.exists():
                    await delete_preview(pp)

            _counter[0] += len(batch)
            await broadcast("tag_progress", done=_counter[0], total=total)

    # Chunk rows into YOLO-batch-sized groups and fan them out concurrently.
    batches = [rows[i : i + _TAG_BATCH_SIZE] for i in range(0, len(rows), _TAG_BATCH_SIZE)]
    await asyncio.gather(*[_process_batch(b) for b in batches])

    await broadcast("phase_complete", phase="tag", tagged=_counter[0])
    logger.info("Phase 4 complete: %d files tagged", _counter[0])


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
