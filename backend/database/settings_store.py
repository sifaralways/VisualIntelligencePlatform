"""
VIP — Persisted ML settings store.

All tunable ML/detection parameters live in the `app_settings` DB table so
the user can tweak them from the Admin UI without touching code.

Usage in ML code (synchronous, from executor threads):
    from backend.database.settings_store import get as get_setting
    threshold = get_setting('face_detection_threshold')

Usage in async routes:
    from backend.database.settings_store import load_cache, update, reset_all
    await load_cache()          # refresh in-process cache from DB
    await update({'key': val})  # persist + refresh cache
    await reset_all()           # restore all defaults + refresh cache
"""

from __future__ import annotations

import logging
from typing import Any

from backend.database.db import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parameter catalogue — single source of truth for defaults + UI metadata
# ---------------------------------------------------------------------------
# Each entry: default value, type, min, max, step, human label, description, group.
# type is one of "float" | "int".

DEFAULTS: dict[str, dict[str, Any]] = {
    "face_detection_threshold": {
        "value": 0.6, "type": "float", "min": 0.3, "max": 0.99, "step": 0.05,
        "label": "Face detection confidence",
        "description": "RetinaFace minimum confidence score. Raise to reduce false detections; lower to catch distant/occluded faces.",
        "group": "Face Detection",
    },
    "min_face_size_px": {
        "value": 60, "type": "int", "min": 20, "max": 200, "step": 10,
        "label": "Min face size (px)",
        "description": "Ignore face detections smaller than this. Raise to skip tiny background faces.",
        "group": "Face Detection",
    },
    "gender_min_sharpness": {
        "value": 15.0, "type": "float", "min": 0.0, "max": 50.0, "step": 5.0,
        "label": "Gender / age min sharpness",
        "description": "Suppress gender and age predictions when the face crop sharpness (0–100) is below this. Blurry crops produce near-random results.",
        "group": "Face Detection",
    },
    "hdbscan_min_cluster_size": {
        "value": 2, "type": "int", "min": 2, "max": 10, "step": 1,
        "label": "Min cluster size",
        "description": "Minimum number of faces required to form a cluster. 2 allows pairs to cluster.",
        "group": "Clustering",
    },
    "hdbscan_min_samples": {
        "value": 1, "type": "int", "min": 1, "max": 10, "step": 1,
        "label": "HDBSCAN min samples",
        "description": "Controls how conservative clustering is. 1 = permissive (fewer noise singletons). Raise to tighten.",
        "group": "Clustering",
    },
    "hdbscan_cluster_epsilon": {
        "value": 0.04, "type": "float", "min": 0.0, "max": 0.30, "step": 0.01,
        "label": "Cluster merge epsilon",
        "description": "Cosine distance below which HDBSCAN merges sub-clusters. 0.04 ≈ similarity 0.96. Raise to merge same-person sub-clusters; keep low to avoid merging different people.",
        "group": "Clustering",
    },
    "cluster_inertia_threshold": {
        "value": 0.85, "type": "float", "min": 0.5, "max": 0.99, "step": 0.05,
        "label": "Cluster validity threshold",
        "description": "Clusters whose mean intra-similarity falls below this are rejected and split into singletons. Raise to be stricter about cluster quality.",
        "group": "Clustering",
    },
    "high_confidence_threshold": {
        "value": 0.92, "type": "float", "min": 0.7, "max": 0.99, "step": 0.01,
        "label": "High-confidence threshold",
        "description": "Clusters above this intra-similarity score show a green tick in the People tab.",
        "group": "Clustering",
    },
    "yolo_conf_threshold": {
        "value": 0.50, "type": "float", "min": 0.1, "max": 0.95, "step": 0.05,
        "label": "Object detection confidence",
        "description": "YOLOv11 minimum confidence. Raise to reduce false object tags; lower to detect more objects.",
        "group": "Object Detection",
    },
    "places365_top_k": {
        "value": 5, "type": "int", "min": 1, "max": 20, "step": 1,
        "label": "Scene top-K",
        "description": "Number of scene candidates Places365 evaluates. Higher means more detail but slower.",
        "group": "Scene & Tags",
    },
    "landmark_threshold": {
        "value": 0.26, "type": "float", "min": 0.1, "max": 0.9, "step": 0.05,
        "label": "Landmark similarity threshold",
        "description": "CLIP minimum cosine similarity required to tag a location as a known landmark.",
        "group": "Scene & Tags",
    },
    "species_threshold": {
        "value": 0.30, "type": "float", "min": 0.1, "max": 0.9, "step": 0.05,
        "label": "Species similarity threshold",
        "description": "BioCLIP minimum cosine similarity required to identify an animal species.",
        "group": "Scene & Tags",
    },
}

# ---------------------------------------------------------------------------
# In-process cache — populated from DB at pipeline start and on every write.
# ML code runs synchronously in executor threads so it reads from this dict.
# ---------------------------------------------------------------------------
_cache: dict[str, Any] = {k: v["value"] for k, v in DEFAULTS.items()}


def get(key: str) -> Any:
    """
    Synchronous read — safe to call from executor threads.
    Falls back to the default value if the key is unknown.
    """
    return _cache.get(key, DEFAULTS.get(key, {}).get("value"))


# ---------------------------------------------------------------------------
# Async helpers — called from API routes and pipeline entry point
# ---------------------------------------------------------------------------

async def load_cache() -> None:
    """Reload all settings from the DB into the in-process cache."""
    global _cache
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT key, value FROM app_settings")
    merged = {k: v["value"] for k, v in DEFAULTS.items()}
    for row in rows:
        key = row["key"]
        if key in DEFAULTS:
            merged[key] = _cast(key, row["value"])
    _cache = merged
    logger.debug("Settings cache loaded: %s", _cache)


async def get_all() -> list[dict]:
    """
    Return all settings with current values + metadata for the admin UI.
    Groups are returned in a stable order.
    """
    await load_cache()
    result = []
    for key, meta in DEFAULTS.items():
        result.append({
            "key": key,
            "value": _cache.get(key, meta["value"]),
            "default": meta["value"],
            "type": meta["type"],
            "min": meta["min"],
            "max": meta["max"],
            "step": meta["step"],
            "label": meta["label"],
            "description": meta["description"],
            "group": meta["group"],
        })
    return result


async def update(updates: dict[str, Any]) -> None:
    """
    Persist one or more settings to the DB, then refresh the cache.
    Unknown keys are silently ignored.
    """
    valid = {k: v for k, v in updates.items() if k in DEFAULTS}
    if not valid:
        return
    async with get_db() as db:
        for key, value in valid.items():
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value)),
            )
    await load_cache()
    logger.info("Settings updated: %s", valid)


async def reset_all() -> None:
    """Restore every setting to its default value."""
    async with get_db() as db:
        for key, meta in DEFAULTS.items():
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(meta["value"])),
            )
    await load_cache()
    logger.info("Settings reset to defaults")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cast(key: str, raw: str) -> Any:
    """Cast a raw string value from the DB to the correct Python type."""
    t = DEFAULTS.get(key, {}).get("type", "float")
    try:
        return int(raw) if t == "int" else float(raw)
    except (ValueError, TypeError):
        return DEFAULTS[key]["value"]
