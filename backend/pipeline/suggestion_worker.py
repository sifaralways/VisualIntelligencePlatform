from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from backend.api.websocket import broadcast
from backend.database import settings_store
from backend.database.db import get_db
from backend.face_quality import face_sample_weight as _shared_face_sample_weight, weighted_centroid_from_rows as _shared_weighted_centroid_from_rows
from backend.pipeline.centroid import load_centroid
from backend.runtime.activity import seconds_since_user_activity

logger = logging.getLogger(__name__)


def _cfg_bool(key: str, default: bool) -> bool:
    return bool(int(settings_store.get(key) if settings_store.get(key) is not None else int(default)))


def _cfg_int(key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(settings_store.get(key))
    except Exception:
        value = default
    return max(low, min(high, value))


def _cfg_float(key: str, default: float, low: float, high: float) -> float:
    try:
        value = float(settings_store.get(key))
    except Exception:
        value = default
    return max(low, min(high, value))


def _normalise_vector(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    if norm > 0:
        return vec / norm
    return vec


def _face_sample_weight(detection_conf: float | None, bbox_w: float | None, bbox_h: float | None) -> float:
    return _shared_face_sample_weight(detection_conf, bbox_w, bbox_h)


def _weighted_centroid_from_rows(rows: list) -> np.ndarray | None:
    return _shared_weighted_centroid_from_rows(rows)


def _best_competing_person(cluster_centroid: np.ndarray, competitor_centroids: list[tuple[int, np.ndarray]]) -> tuple[int | None, float | None]:
    if not competitor_centroids:
        return None, None
    best_id: int | None = None
    best_sim: float | None = None
    for other_id, other_vec in competitor_centroids:
        sim = float(np.dot(cluster_centroid, other_vec))
        if best_sim is None or sim > best_sim:
            best_id = other_id
            best_sim = sim
    return best_id, best_sim


async def _load_person_ids_for_refresh(batch_size: int) -> list[int]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT p.id
            FROM persons p
                        LEFT JOIN person_suggestion_queue q
                            ON q.person_id = p.id
                         AND q.source = 'quality_background'
            WHERE p.name IS NOT NULL
              AND p.is_merged = 0
              AND p.is_ignored = 0
                        GROUP BY p.id
                        ORDER BY COALESCE(MAX(q.generated_at), '1970-01-01 00:00:00') ASC, p.id ASC
            LIMIT ?
            """,
            (batch_size,),
        )
    return [int(r["id"]) for r in rows]


async def _refresh_person_queue_quality(person_id: int, *, max_per_person: int, min_sim: float, min_margin: float) -> int:
    async with get_db() as db:
        person = await (
            await db.execute(
                "SELECT id, centroid FROM persons WHERE id=? AND is_merged=0 AND is_ignored=0",
                (person_id,),
            )
        ).fetchone()
        if not person:
            return 0

        emb_rows = await db.execute_fetchall(
            """
            SELECT e.vector, f.detection_conf, f.bbox_w, f.bbox_h,
                   f.face_attributes, f.face_sharpness, f.pose_yaw, f.pose_pitch, f.pose_roll
            FROM embeddings e
            JOIN faces f ON f.id = e.face_id
            JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
            JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
            JOIN persons p ON p.person_guid = cpc.person_guid
            WHERE p.id = ?
            """,
            (person_id,),
        )

        person_centroid = _weighted_centroid_from_rows(emb_rows)
        if person_centroid is None and person["centroid"]:
            person_centroid = _normalise_vector(load_centroid(person["centroid"]))
        if person_centroid is None:
            await db.execute(
                "DELETE FROM person_suggestion_queue WHERE person_id=? AND status='pending' AND source='quality_background'",
                (person_id,),
            )
            return 0

        candidate_rows = await db.execute_fetchall(
            """
            SELECT c.id AS cluster_id,
                   c.centroid,
                   c.member_count,
                   COALESCE(MIN(f_current.thumbnail_path), MIN(f_legacy.thumbnail_path)) AS representative_thumbnail
            FROM clusters c
            LEFT JOIN v_face_cluster_current fcc ON fcc.cluster_guid = c.cluster_guid
            LEFT JOIN faces f_current ON f_current.face_guid = fcc.face_guid AND f_current.thumbnail_path IS NOT NULL
            LEFT JOIN faces f_legacy ON f_legacy.cluster_id = c.id AND f_legacy.thumbnail_path IS NOT NULL
            LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = c.cluster_guid
            LEFT JOIN persons owner_person ON owner_person.person_guid = cpc.person_guid AND owner_person.is_merged = 0
            WHERE owner_person.id IS NULL
              AND c.centroid IS NOT NULL
            GROUP BY c.id
            HAVING representative_thumbnail IS NOT NULL
            """,
        )

        competitor_rows = await db.execute_fetchall(
            """
            SELECT id, centroid
            FROM persons
            WHERE id != ?
              AND is_merged = 0
              AND is_ignored = 0
              AND centroid IS NOT NULL
              AND COALESCE(centroid_n, 0) > 0
            """,
            (person_id,),
        )
        blocked_rows = await db.execute_fetchall(
            """
            SELECT CASE WHEN person_a_id=? THEN person_b_id ELSE person_a_id END AS other_id
            FROM person_cannot_link
            WHERE person_a_id=? OR person_b_id=?
            """,
            (person_id, person_id, person_id),
        )
        blocked_ids = {int(r["other_id"]) for r in blocked_rows}

        rejected_rows = await db.execute_fetchall(
            "SELECT cluster_id FROM rejected_suggestions WHERE person_id=?",
            (person_id,),
        )
        rejected_cluster_ids = {int(r["cluster_id"]) for r in rejected_rows}

        same_photo_conflict_rows = await db.execute_fetchall(
            """
            SELECT DISTINCT fc.cluster_id
            FROM faces fp
            JOIN faces fc ON fc.media_file_id = fp.media_file_id AND fc.id != fp.id
            JOIN media_files mf ON mf.id = fp.media_file_id
            WHERE fp.person_id = ?
              AND fc.person_id IS NULL
              AND mf.removed_from_app = 0
              AND COALESCE(fp.bbox_w, 0) * COALESCE(fp.bbox_h, 0) >= 0.015
              AND COALESCE(fc.bbox_w, 0) * COALESCE(fc.bbox_h, 0) >= 0.015
            """,
            (person_id,),
        )
        same_photo_conflict_cluster_ids = {
            int(r["cluster_id"]) for r in same_photo_conflict_rows if r["cluster_id"] is not None
        }

        # Ensure background quality suggestions never undercut the interactive merge threshold.
        suggest_threshold = _cfg_float("merge_suggest_threshold", 0.50, 0.20, 0.99)
        effective_min_sim = max(min_sim, suggest_threshold)

        competitor_centroids = [
            (int(r["id"]), _normalise_vector(load_centroid(r["centroid"])))
            for r in competitor_rows
            if r["centroid"]
        ]

        scored: list[tuple[int, float, int | None, float | None, float | None, int, str | None]] = []
        for row in candidate_rows:
            centroid_blob = row["centroid"]
            if not centroid_blob:
                continue
            cluster_id = int(row["cluster_id"])
            if cluster_id in rejected_cluster_ids:
                continue
            if cluster_id in same_photo_conflict_cluster_ids:
                continue

            c_vec = _normalise_vector(np.frombuffer(centroid_blob, dtype=np.float32))
            sim = float(np.dot(person_centroid, c_vec))
            if sim < effective_min_sim:
                continue

            competing_id, competing_sim = _best_competing_person(c_vec, competitor_centroids)
            if (
                competing_id is not None
                and competing_id in blocked_ids
                and competing_sim is not None
                and competing_sim >= effective_min_sim
            ):
                continue
            margin = None if competing_sim is None else (sim - competing_sim)
            if margin is not None and margin < min_margin:
                continue

            scored.append(
                (
                    cluster_id,
                    sim,
                    competing_id,
                    competing_sim,
                    margin,
                    int(row["member_count"] or 0),
                    row["representative_thumbnail"],
                )
            )

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:max_per_person]

        await db.execute(
            "DELETE FROM person_suggestion_queue WHERE person_id=? AND status='pending' AND source='quality_background'",
            (person_id,),
        )

        if not top:
            return 0

        await db.executemany(
            """
            INSERT INTO person_suggestion_queue (
                person_id,
                cluster_id,
                similarity,
                competing_person_id,
                competing_similarity,
                margin,
                cluster_member_count,
                cluster_thumbnail_path,
                source,
                status,
                generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'quality_background', 'pending', datetime('now'))
            """,
            [
                (
                    person_id,
                    cid,
                    round(sim, 4),
                    comp_id,
                    round(comp_sim, 4) if comp_sim is not None else None,
                    round(margin, 4) if margin is not None else None,
                    member_count,
                    thumb,
                )
                for (cid, sim, comp_id, comp_sim, margin, member_count, thumb) in top
            ],
        )

        return len(top)


async def run_quality_suggestion_worker() -> None:
    logger.info("Quality suggestion worker started")
    await broadcast("suggestion_worker_started", message="Background suggestion worker started")
    worker_state = "running"

    while True:
        try:
            enabled = _cfg_bool("suggestion_worker_enabled", True)
            idle_sec = _cfg_int("suggestion_worker_idle_sec", 20, 5, 300)
            sleep_sec = _cfg_int("suggestion_worker_sleep_sec", 15, 2, 300)
            person_batch = _cfg_int("suggestion_worker_person_batch", 8, 1, 50)
            max_per_person = _cfg_int("suggestion_worker_max_per_person", 25, 5, 100)
            min_sim = _cfg_float("suggestion_worker_min_sim", 0.45, 0.20, 0.95)
            min_margin = _cfg_float("suggestion_worker_min_margin", 0.02, 0.00, 0.30)

            if not enabled:
                if worker_state != "disabled":
                    worker_state = "disabled"
                    await broadcast(
                        "suggestion_worker_paused",
                        reason="disabled",
                        message="Background suggestion worker disabled in settings",
                    )
                await asyncio.sleep(5)
                continue

            if seconds_since_user_activity() < idle_sec:
                if worker_state != "waiting_for_idle":
                    worker_state = "waiting_for_idle"
                    await broadcast(
                        "suggestion_worker_paused",
                        reason="user_active",
                        message="Background suggestion worker waiting for idle",
                    )
                await asyncio.sleep(2)
                continue

            if worker_state != "running":
                worker_state = "running"
                await broadcast("suggestion_worker_resumed", message="Background suggestion worker resumed")

            started = time.perf_counter()
            person_ids = await _load_person_ids_for_refresh(person_batch)
            generated = 0
            for pid in person_ids:
                generated += await _refresh_person_queue_quality(
                    pid,
                    max_per_person=max_per_person,
                    min_sim=min_sim,
                    min_margin=min_margin,
                )

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "Quality suggestion worker cycle: persons=%d generated=%d elapsed_ms=%d",
                len(person_ids),
                generated,
                elapsed_ms,
            )
            await broadcast(
                "suggestion_worker_cycle",
                persons=len(person_ids),
                generated=generated,
                elapsed_ms=elapsed_ms,
            )
            await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            logger.info("Quality suggestion worker cancelled")
            await broadcast("suggestion_worker_stopped", message="Background suggestion worker stopped")
            raise
        except Exception:
            logger.exception("Quality suggestion worker cycle failed")
            await asyncio.sleep(5)
