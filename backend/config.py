"""
VIP — Visual Intelligence Platform
Configuration constants and paths.

All tunable parameters live here. Nothing is hard-coded elsewhere.
If you tweak a threshold, change it here — not in the code that uses it.
"""

from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # -------------------------------------------------------------------------
    # Application identity
    # -------------------------------------------------------------------------
    app_name: str = "Visual Intelligence Platform"
    app_short_name: str = "VIP"
    version: str = "0.1.0"

    # -------------------------------------------------------------------------
    # Paths — all runtime data lives under APP_SUPPORT_DIR, never in the repo
    # -------------------------------------------------------------------------
    app_support_dir: Path = Path.home() / "Library" / "Application Support" / "VIP"

    @property
    def db_path(self) -> Path:
        return self.app_support_dir / "vip.db"

    @property
    def faiss_path(self) -> Path:
        return self.app_support_dir / "vip.faiss"

    @property
    def thumbnail_dir(self) -> Path:
        """Small face crop JPEGs — permanent residents."""
        return self.app_support_dir / "thumbnails"

    @property
    def photo_thumbs_dir(self) -> Path:
        """Scaled-down photo thumbnails for the UI grid — permanent, one per media file."""
        return self.app_support_dir / "photo_thumbs"

    @property
    def preview_dir(self) -> Path:
        """Extracted JPEG previews from RAW files — temporary, cleared after tagging."""
        return self.app_support_dir / "previews"

    # -------------------------------------------------------------------------
    # API server
    # -------------------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 7474

    # -------------------------------------------------------------------------
    # Supported formats (RAW + direct image formats)
    # -------------------------------------------------------------------------
    supported_formats: frozenset = frozenset({
        # RAW formats
        ".cr3",   # Canon — primary
        ".arw",   # Sony
        ".nef",   # Nikon
        ".dng",   # Adobe / Leica / misc
        ".rw2",   # Panasonic
        ".orf",   # Olympus
        ".raf",   # Fujifilm
        ".cr2",   # Canon legacy
        # Direct image formats
        ".jpg",   # JPEG
        ".jpeg",  # JPEG (alternate extension)
        ".avif",  # AVIF (modern compressed format)
    })

    # Formats that are themselves the full image — no embedded preview extraction.
    # Pillow is used directly to produce the normalised JPEG for ML inference.
    direct_image_formats: frozenset = frozenset({".jpg", ".jpeg", ".avif"})

    # -------------------------------------------------------------------------
    # iCloud stub detection
    # Files smaller than this threshold for a known RAW type = iCloud stub.
    # Real CR3s are 30–60MB. 4KB is a very safe ceiling.
    # -------------------------------------------------------------------------
    stub_max_size_bytes: int = 4096

    # -------------------------------------------------------------------------
    # InsightFace / ML
    # -------------------------------------------------------------------------
    insightface_model: str = "antelopev2"
    embedding_dim: int = 512
    # Raise from 0.5 → 0.6: filter borderline face detections that degrade
    # gender/age accuracy and pollute clusters with unreliable embeddings.
    face_detection_threshold: float = 0.6
    # 60px minimum: ArcFace accuracy degrades noticeably on crops smaller than
    # this; GenderAge predictions on tiny crops are near-random.
    min_face_size_px: int = 60
    # Gate gender/age output: only emit these attributes when the face crop
    # has sufficient sharpness (Laplacian variance proxy, 0–100 scale).
    # Below this threshold the GenderAge model's predictions are unreliable.
    gender_min_sharpness: float = 15.0

    # -------------------------------------------------------------------------
    # Clustering (HDBSCAN)
    # -------------------------------------------------------------------------
    hdbscan_min_cluster_size: int = 2
    # min_samples controls cluster conservatism.  1 = most permissive (every
    # face is a candidate core point).  Keeps same-person singletons from
    # being labelled noise and left unmatched.
    hdbscan_min_samples: int = 1
    # cluster_selection_epsilon (cosine distance): only merge sub-clusters
    # whose centroids are THIS close. cosine_dist = 1 - similarity.
    # 0.04 = similarity 0.96: near-identical centroids only.
    # Previous value 0.20 (similarity 0.80) was merging unrelated people
    # whose subclusters happened to be within that radius — causing a single
    # cluster to swallow all faces from a group photo (intra_sim 0.29-0.43).
    hdbscan_cluster_epsilon: float = 0.04
    # Cosine similarity threshold above which a cluster is "high confidence"
    # i.e., shown as a single tile + count without requiring manual review.
    high_confidence_threshold: float = 0.92
    # Below this mean intra-cluster similarity → uncertain cluster, show grid
    cluster_inertia_threshold: float = 0.85

    # -------------------------------------------------------------------------
    # Person re-identification thresholds (used in Phase 3b auto-name)
    # -------------------------------------------------------------------------
    # Cosine similarity >= this → auto-assign person name silently (very high
    # confidence). Default 0.98 means near-identical embedding centroid.
    auto_name_threshold: float = 0.98
    # Cosine similarity >= this (but below auto_name_threshold) → pop up a
    # "Same person?" suggestion card for the user to confirm or reject.
    merge_suggest_threshold: float = 0.63

    # -------------------------------------------------------------------------
    # FAISS
    # -------------------------------------------------------------------------
    # Use flat (exact) index up to ~300K vectors. Switch to IVF beyond that.
    faiss_use_ivf_above: int = 300_000
    faiss_ivf_nlist: int = 256

    # -------------------------------------------------------------------------
    # Batch sizes — tuned for M2 Max with 64GB unified memory
    # -------------------------------------------------------------------------
    embed_batch_size: int = 32      # faces per ML inference batch
    exif_batch_size: int = 100      # files per ExifTool stay_open batch
    scan_worker_concurrency: int = 8  # async file I/O concurrency

    # -------------------------------------------------------------------------
    # ExifTool
    # -------------------------------------------------------------------------
    exiftool_timeout_sec: int = 30
    # Write metadata in-place. Set to True to keep _original backup files.
    exiftool_write_backup: bool = False

    # -------------------------------------------------------------------------
    # Thermal management — sleep between batches if Mac is running hot
    # -------------------------------------------------------------------------
    thermal_sleep_sec: float = 2.0

    # -------------------------------------------------------------------------
    # Tagging models (Phase 4)
    # -------------------------------------------------------------------------
    # yolov8n-oiv7.pt: Open Images V7, 600 object classes (vs COCO 80).
    # Auto-downloads from Ultralytics Hub on first start (~6 MB).
    # Override: VIP_YOLO_MODEL=yolov8s-oiv7.pt for higher accuracy.
    yolo_model: str = "yolov8n-oiv7.pt"
    yolo_conf_threshold: float = 0.50    # YOLO minimum detection confidence
    # Landmark threshold:
    #   CLIP ViT-L/14 (default): cosine similarity — 0.28 is a good starting point.
    #   GLDv2 EfficientNet (if configured via env vars): softmax probability — use 0.05-0.15.
    landmark_threshold: float = 0.28     # CLIP cosine-sim / GLDv2 softmax probability
    species_threshold: float = 0.30      # BioCLIP minimum cosine similarity for species
    places365_top_k: int = 5             # Places365 top-k scenes to evaluate

    class Config:
        env_prefix = "VIP_"   # override any setting with VIP_API_PORT=8080 etc.


# Singleton — import this everywhere
settings = Settings()


def ensure_dirs() -> None:
    """Create all required application support directories."""
    for d in [
        settings.app_support_dir,
        settings.thumbnail_dir,
        settings.preview_dir,
        settings.photo_thumbs_dir,
    ]:
        d.mkdir(parents=True, exist_ok=True)
