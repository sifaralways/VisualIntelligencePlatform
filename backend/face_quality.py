from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import numpy as np


def _normalise_vector(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    if norm > 0:
        return vec / norm
    return vec


def parse_face_attributes(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_quality_fields(raw: str | None) -> dict[str, float | None]:
    attrs = parse_face_attributes(raw)
    pose = attrs.get("Pose") or {}
    quality = attrs.get("Quality") or {}
    return {
        "face_sharpness": _safe_float(quality.get("Sharpness")),
        "pose_yaw": _safe_float(pose.get("Yaw")),
        "pose_pitch": _safe_float(pose.get("Pitch")),
        "pose_roll": _safe_float(pose.get("Roll")),
    }


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def row_quality_values(row: Any) -> dict[str, float | None]:
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    face_sharpness = row["face_sharpness"] if "face_sharpness" in keys else None
    pose_yaw = row["pose_yaw"] if "pose_yaw" in keys else None
    pose_pitch = row["pose_pitch"] if "pose_pitch" in keys else None
    pose_roll = row["pose_roll"] if "pose_roll" in keys else None
    if all(value is not None for value in (face_sharpness, pose_yaw, pose_pitch, pose_roll)):
        return {
            "face_sharpness": _safe_float(face_sharpness),
            "pose_yaw": _safe_float(pose_yaw),
            "pose_pitch": _safe_float(pose_pitch),
            "pose_roll": _safe_float(pose_roll),
        }

    raw_attrs = row["face_attributes"] if "face_attributes" in keys else None
    fallback = extract_quality_fields(raw_attrs)
    return {
        "face_sharpness": _safe_float(face_sharpness) if face_sharpness is not None else fallback["face_sharpness"],
        "pose_yaw": _safe_float(pose_yaw) if pose_yaw is not None else fallback["pose_yaw"],
        "pose_pitch": _safe_float(pose_pitch) if pose_pitch is not None else fallback["pose_pitch"],
        "pose_roll": _safe_float(pose_roll) if pose_roll is not None else fallback["pose_roll"],
    }


def face_sample_weight(
    detection_conf: float | None,
    bbox_w: float | None,
    bbox_h: float | None,
    face_sharpness: float | None = None,
    pose_yaw: float | None = None,
    pose_pitch: float | None = None,
    pose_roll: float | None = None,
) -> float:
    conf = float(detection_conf) if detection_conf is not None else 0.5
    conf = max(0.05, min(1.0, conf))

    area = 0.0
    if bbox_w is not None and bbox_h is not None:
        area = max(0.0, float(bbox_w) * float(bbox_h))
    size_weight = max(0.4, min(1.6, area / 0.02 if area > 0 else 0.7))

    sharpness_weight = 1.0
    if face_sharpness is not None:
        sharpness_weight = max(0.55, min(1.45, float(face_sharpness) / 35.0))

    pose_weight = 1.0
    pose_components = [value for value in (pose_yaw, pose_pitch, pose_roll) if value is not None]
    if pose_components:
        yaw = abs(float(pose_yaw or 0.0)) / 45.0
        pitch = abs(float(pose_pitch or 0.0)) / 35.0
        roll = abs(float(pose_roll or 0.0)) / 25.0
        pose_penalty = max(yaw, pitch, roll)
        pose_weight = max(0.55, min(1.15, 1.15 - 0.45 * pose_penalty))

    return max(0.05, conf * size_weight * sharpness_weight * pose_weight)


def face_quality_score_from_row(row: Any) -> float:
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    quality = row_quality_values(row)
    return face_sample_weight(
        row["detection_conf"] if "detection_conf" in keys else None,
        row["bbox_w"] if "bbox_w" in keys else None,
        row["bbox_h"] if "bbox_h" in keys else None,
        quality["face_sharpness"],
        quality["pose_yaw"],
        quality["pose_pitch"],
        quality["pose_roll"],
    )


def _row_date_taken(row: Any) -> str | None:
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    if "date_taken" not in keys:
        return None
    value = row["date_taken"]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date_taken_timestamp(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass

    for fmt in (
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except Exception:
            continue
    return None


def _compute_row_recency_scores(rows: list) -> list[float]:
    timestamps = [_parse_date_taken_timestamp(_row_date_taken(row)) for row in rows]
    valid = [ts for ts in timestamps if ts is not None]
    if len(valid) < 2:
        return [0.0] * len(rows)

    t_min = min(valid)
    t_max = max(valid)
    if t_max <= t_min:
        return [0.0] * len(rows)

    span = t_max - t_min
    scores: list[float] = []
    for ts in timestamps:
        if ts is None:
            scores.append(0.0)
            continue
        scores.append(float((ts - t_min) / span))
    return scores


def select_top_face_rows(
    rows: list,
    max_faces: int,
    *,
    prefer_recent_photos: bool = False,
    recency_boost: float = 0.35,
) -> list:
    if max_faces <= 0 or len(rows) <= max_faces:
        return list(rows)

    if not prefer_recent_photos:
        ranked = sorted(rows, key=face_quality_score_from_row, reverse=True)
        return ranked[:max_faces]

    recency_scores = _compute_row_recency_scores(rows)

    def _combined_rank(entry: tuple[Any, float]) -> float:
        row, recency = entry
        quality = face_quality_score_from_row(row)
        return quality * (1.0 + max(0.0, recency_boost) * recency)

    ranked_pairs = sorted(zip(rows, recency_scores), key=_combined_rank, reverse=True)
    ranked = [row for row, _ in ranked_pairs]
    return ranked[:max_faces]


def weighted_centroid_from_rows(
    rows: list,
    *,
    prefer_recent_photos: bool = False,
    recency_boost: float = 0.35,
) -> np.ndarray | None:
    if not rows:
        return None

    vecs: list[np.ndarray] = []
    weights: list[float] = []
    recency_scores = _compute_row_recency_scores(rows) if prefer_recent_photos else [0.0] * len(rows)
    for row, recency in zip(rows, recency_scores):
        vec = np.frombuffer(row["vector"], dtype=np.float32)
        vecs.append(vec)
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        quality = row_quality_values(row)
        base_weight = face_sample_weight(
            row["detection_conf"] if "detection_conf" in keys else None,
            row["bbox_w"] if "bbox_w" in keys else None,
            row["bbox_h"] if "bbox_h" in keys else None,
            quality["face_sharpness"],
            quality["pose_yaw"],
            quality["pose_pitch"],
            quality["pose_roll"],
        )
        weights.append(base_weight * (1.0 + max(0.0, recency_boost) * recency))

    arr = np.stack(vecs)
    w = np.asarray(weights, dtype=np.float32)
    if np.all(w <= 0):
        centroid = arr.mean(axis=0)
    else:
        centroid = np.average(arr, axis=0, weights=w)
    return _normalise_vector(centroid)