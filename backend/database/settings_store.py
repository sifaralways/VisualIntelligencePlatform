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
from backend.profiles import get_current_profile_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parameter catalogue — single source of truth for defaults + UI metadata
# ---------------------------------------------------------------------------
# Each entry: default value, type, min, max, step, human label, description, group.
# type is one of "float" | "int" | "bool".

DEFAULTS: dict[str, dict[str, Any]] = {
    "face_detection_mode": {
        "value": 0, "type": "int", "min": 0, "max": 2, "step": 1,
        "label": "Face detection mode",
        "description": (
            "Accuracy: CPU-only, 1280×1280 grid — finds every face including small/distant ones, ~1.2 s/photo. "
            "Performance: CoreML ANE/GPU, 640×640 grid — up to 10× faster, may miss very small faces. "
            "Intelligent: starts fast (640/ANE) and auto-escalates to 1280/CPU for wide-angle shots and "
            "crowded scenes with small faces. Best for mixed photo libraries. "
            "Change takes effect on the next scan."
        ),
        "group": "Face Detection",
        "options": [
            {"value": 0, "label": "Accuracy"},
            {"value": 1, "label": "Performance"},
            {"value": 2, "label": "Intelligent"},
        ],
    },
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
    "face_min_sharpness": {
        "value": 20.0, "type": "float", "min": 0.0, "max": 100.0, "step": 5.0,
        "label": "Min face sharpness",
        "description": (
            "Discard face detections whose sharpness score falls below this. "
            "Sharpness is measured on the tight face crop resized to 128×128 using Laplacian variance (0–100). "
            "Bokeh / depth-of-field blurred faces typically score 5–15; "
            "clearly in-focus faces typically score 60–100. "
            "20 removes most out-of-focus background faces while keeping anything recognisably sharp. "
            "0 = accept everything (original behaviour). Takes effect on the next scan."
        ),
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
    "auto_name_threshold": {
        "value": 0.98, "type": "float", "min": 0.70, "max": 0.99, "step": 0.01,
        "label": "Auto-merge threshold",
        "description": (
            "Cosine similarity at or above which an unnamed cluster is automatically merged into a named person "
            "without asking. 0.98 = near-identical (very safe). Lower to ~0.80–0.85 to auto-accept most "
            "merge suggestions; lower still risks merging different people. Must be ≥ Merge-suggest threshold."
        ),
        "group": "Clustering",
    },
    "merge_suggest_threshold": {
        "value": 0.63, "type": "float", "min": 0.40, "max": 0.97, "step": 0.01,
        "label": "Merge-suggest threshold",
        "description": (
            "Cosine similarity at or above which a \"Same person?\" suggestion card is shown. "
            "Matches below this are ignored. Must be ≤ Auto-merge threshold."
        ),
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
    "log_level": {
        "value": 1, "type": "int", "min": 0, "max": 2, "step": 1,
        "label": "Log level",
        "description": (
            "Error: only errors and warnings are recorded. "
            "Info: phase start/end and summary stats (recommended). "
            "Debug: full verbose output — every file, face, and decision."
        ),
        "group": "System",
        "options": [
            {"value": 0, "label": "Error"},
            {"value": 1, "label": "Info"},
            {"value": 2, "label": "Debug"},
        ],
    },
    "embed_concurrency": {
        "value": 4, "type": "int", "min": 1, "max": 16, "step": 1,
        "label": "Phase 2 parallel workers",
        "description": (
            "Number of photos processed simultaneously during Phase 2 (face detection & embedding). "
            "Higher values finish faster but use more CPU, memory, and thermal budget. "
            "Reduce to 1-2 if your device becomes hot or unresponsive during a pipeline run. "
            "Change takes effect on the next scan."
        ),
        "group": "System",
    },
    "tag_concurrency": {
        "value": 2, "type": "int", "min": 1, "max": 8, "step": 1,
        "label": "Phase 4 parallel workers",
        "description": (
            "Number of 16-image YOLO batches processed simultaneously during Phase 4 (object/scene tagging). "
            "Each batch runs YOLO in one GPU forward pass. "
            "1-2 is safe for all Macs; 3-4 on M2 Max / Mac Pro with ample thermal headroom. "
            "Change takes effect on the next scan."
        ),
        "group": "System",
    },
    "florence_concurrency": {
        "value": 1, "type": "int", "min": 1, "max": 4, "step": 1,
        "label": "Florence parallel workers",
        "description": (
            "How many Florence batch jobs run at once during Phase 4. "
            "Start with 1 for stability. Increase to 2 only if florence_wait is low and memory headroom is healthy."
        ),
        "group": "System",
    },
    "florence_inference_batch_size": {
        "value": 8, "type": "int", "min": 1, "max": 16, "step": 1,
        "label": "Florence micro-batch size",
        "description": (
            "Images processed together inside one Florence generation call. "
            "Higher values can improve MPS utilization but increase memory use."
        ),
        "group": "System",
    },
    "florence_num_beams": {
        "value": 1, "type": "int", "min": 1, "max": 4, "step": 1,
        "label": "Florence beam width",
        "description": (
            "Generation beam width for Florence. 1 is fastest. Higher values may improve text quality but can be much slower."
        ),
        "group": "System",
    },
    "exif_batch_size": {
        "value": 500, "type": "int", "min": 10, "max": 1000, "step": 10,
        "label": "Phase 1 ExifTool batch size",
        "description": (
            "Number of files passed to ExifTool in a single subprocess call during Phase 1 (scan). "
            "Larger batches are faster on local SSDs (fewer Perl start-ups). "
            "Reduce to 50–100 if your library is on a NAS or if Phase 1 times out."
        ),
        "group": "System",
    },
    "nudenet_confidence_threshold": {
        "value": 0.65, "type": "float", "min": 0.1, "max": 0.95, "step": 0.05,
        "label": "Explicit content confidence threshold",
        "description": (
            "NudeNet minimum detection confidence to store an explicit label. "
            "Lower values detect more but increase false positives. "
            "0.65 is a reliable default. Takes effect on the next scan."
        ),
        "group": "Content Safety",
    },
    "object_detector_enabled": {
        "value": 1, "type": "bool", "min": 0, "max": 1, "step": 1,
        "label": "Object detector enabled",
        "description": "Enable YOLO-based object and animal base detection during Phase 4 tagging.",
        "group": "ML Modules",
        "options": [
            {"value": 0, "label": "Off"},
            {"value": 1, "label": "On"},
        ],
    },
    "scene_classifier_enabled": {
        "value": 1, "type": "bool", "min": 0, "max": 1, "step": 1,
        "label": "Scene classifier enabled",
        "description": "Enable Places365 scene and geography tagging.",
        "group": "ML Modules",
        "options": [
            {"value": 0, "label": "Off"},
            {"value": 1, "label": "On"},
        ],
    },
    "landmark_recogniser_enabled": {
        "value": 1, "type": "bool", "min": 0, "max": 1, "step": 1,
        "label": "Landmark recogniser enabled",
        "description": "Enable landmark recognition using GLDv2 or OpenCLIP fallback.",
        "group": "ML Modules",
        "options": [
            {"value": 0, "label": "Off"},
            {"value": 1, "label": "On"},
        ],
    },
    "species_classifier_enabled": {
        "value": 1, "type": "bool", "min": 0, "max": 1, "step": 1,
        "label": "Species classifier enabled",
        "description": "Enable BioCLIP species classification when an animal is detected.",
        "group": "ML Modules",
        "options": [
            {"value": 0, "label": "Off"},
            {"value": 1, "label": "On"},
        ],
    },
    "geo_resolver_enabled": {
        "value": 1, "type": "bool", "min": 0, "max": 1, "step": 1,
        "label": "Geo resolver enabled",
        "description": "Enable GPS-to-place resolution for photos with location coordinates.",
        "group": "ML Modules",
        "options": [
            {"value": 0, "label": "Off"},
            {"value": 1, "label": "On"},
        ],
    },
    "explicit_detector_enabled": {
        "value": 1, "type": "bool", "min": 0, "max": 1, "step": 1,
        "label": "Explicit detector enabled",
        "description": "Enable NudeNet explicit-content detection during tagging.",
        "group": "ML Modules",
        "options": [
            {"value": 0, "label": "Off"},
            {"value": 1, "label": "On"},
        ],
    },
    "florence_enabled": {
        "value": 0, "type": "bool", "min": 0, "max": 1, "step": 1,
        "label": "Florence enabled",
        "description": "Enable Florence-2 captioning and OCR enrichment during Phase 4 tagging.",
        "group": "ML Modules",
        "options": [
            {"value": 0, "label": "Off"},
            {"value": 1, "label": "On"},
        ],
    },
    "exif_batch_timeout": {
        "value": 300, "type": "int", "min": 30, "max": 3600, "step": 30,
        "label": "Phase 1 ExifTool timeout (s)",
        "description": (
            "Maximum seconds ExifTool is allowed to run for a single batch before it is abandoned and "
            "that batch's EXIF fields are left empty. "
            "Increase if you see 'ExifTool batch timed out' warnings — typically needed for very large "
            "RAW files (CR3, ARW) over a slow network. "
            "A good rule of thumb: batch size × 1 s, doubled for NAS headroom."
        ),
        "group": "System",
    },
}

# ---------------------------------------------------------------------------
# In-process cache — populated from DB at pipeline start and on every write.
# ML code runs synchronously in executor threads so it reads from this dict.
# ---------------------------------------------------------------------------
_DEFAULT_CACHE: dict[str, Any] = {k: v["value"] for k, v in DEFAULTS.items()}
_cache_by_profile: dict[str, dict[str, Any]] = {}


def get(key: str) -> Any:
    """
    Synchronous read — safe to call from executor threads.
    Falls back to the default value if the key is unknown.
    """
    cache = _cache_by_profile.get(get_current_profile_id(), _DEFAULT_CACHE)
    return cache.get(key, DEFAULTS.get(key, {}).get("value"))


# ---------------------------------------------------------------------------
# Async helpers — called from API routes and pipeline entry point
# ---------------------------------------------------------------------------

async def load_cache() -> None:
    """Reload all settings from the DB into the in-process cache."""
    profile_id = get_current_profile_id()
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT key, value FROM app_settings")
    merged = {k: v["value"] for k, v in DEFAULTS.items()}
    for row in rows:
        key = row["key"]
        if key in DEFAULTS:
            merged[key] = _cast(key, row["value"])
    _cache_by_profile[profile_id] = merged
    logger.debug("Settings cache loaded for profile %s: %s", profile_id, merged)


async def get_all() -> list[dict]:
    """
    Return all settings with current values + metadata for the admin UI.
    Groups are returned in a stable order.
    """
    await load_cache()
    cache = _cache_by_profile.get(get_current_profile_id(), _DEFAULT_CACHE)
    result = []
    for key, meta in DEFAULTS.items():
        result.append({
            "key": key,
            "value": cache.get(key, meta["value"]),
            "default": meta["value"],
            "type": meta["type"],
            "min": meta["min"],
            "max": meta["max"],
            "step": meta["step"],
            "label": meta["label"],
            "description": meta["description"],
            "group": meta["group"],
            "options": meta.get("options"),  # list[{value, label}] for segmented-control UI, or None
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
    logger.info("Settings updated for profile %s: %s", get_current_profile_id(), valid)


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
    logger.info("Settings reset to defaults for profile %s", get_current_profile_id())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cast(key: str, raw: str) -> Any:
    """Cast a raw string value from the DB to the correct Python type."""
    t = DEFAULTS.get(key, {}).get("type", "float")
    try:
        if t in ("int", "bool"):
            return int(raw)
        return float(raw)
    except (ValueError, TypeError):
        return DEFAULTS[key]["value"]
