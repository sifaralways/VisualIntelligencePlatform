"""
VIP ML — Tagging orchestrator.

Coordinates all secondary ML models:
  • ObjectDetector     (YOLOv11)       → objects + common animals
  • SceneClassifier    (Places365)     → geography + built places
  • LandmarkRecogniser (OpenCLIP)      → famous landmarks
  • SpeciesClassifier  (BioCLIP)       → species-level animal ID
  • GeoResolver        (Nominatim)     → GPS → place name

All models are loaded lazily on first use. Models that fail to load
(e.g. missing pip package) are silently skipped so the pipeline still runs.

Result contract:
  TagResult.objects    : list[str]   — e.g. ["Car", "Laptop"]
  TagResult.animals    : list[str]   — e.g. ["Dog", "Golden Retriever"]
  TagResult.geography  : list[str]   — e.g. ["Beach", "Ocean"]
  TagResult.places     : list[str]   — e.g. ["Eiffel Tower", "Paris"]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.ml.object_detector import ObjectDetector
from backend.ml.scene_classifier import SceneClassifier
from backend.ml.landmark_recogniser import LandmarkRecogniser
from backend.ml.species_classifier import SpeciesClassifier
from backend.ml.geo_resolver import GeoResolver

logger = logging.getLogger(__name__)


@dataclass
class TagResult:
    objects: list[str]   = field(default_factory=list)
    animals: list[str]   = field(default_factory=list)
    geography: list[str] = field(default_factory=list)
    places: list[str]    = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([self.objects, self.animals, self.geography, self.places])


class Tagger:
    """
    Single entry-point for all tagging models.
    Instantiate once; call load() before first use.
    """

    def __init__(self) -> None:
        self._object_detector   = ObjectDetector()
        self._scene_classifier  = SceneClassifier()
        self._landmark          = LandmarkRecogniser()
        self._species           = SpeciesClassifier()
        self._geo               = GeoResolver()
        self._loaded            = False

    def load(self) -> None:
        """Load all models. Failures are logged but don't abort."""
        if self._loaded:
            return
        logger.info("Loading tagging models …")
        for model in (
            self._object_detector,
            self._scene_classifier,
            self._landmark,
            self._species,
            self._geo,
        ):
            try:
                model.load()
            except Exception as e:
                logger.error("Model load error: %s", e)
        self._loaded = True
        logger.info("Tagging models ready")

    def tag(
        self,
        image_path: Path,
        gps_lat: Optional[float] = None,
        gps_lon: Optional[float] = None,
    ) -> TagResult:
        """
        Run all applicable tagging models on an image.

        Args:
            image_path: Path to a JPEG image (preview or full).
            gps_lat:    GPS latitude from EXIF, if available.
            gps_lon:    GPS longitude from EXIF, if available.

        Returns:
            TagResult with all detected tags across categories.
        """
        result = TagResult()

        if not image_path.exists():
            logger.warning("Image not found for tagging: %s", image_path)
            return result

        # ── Objects + Animals (YOLO) ────────────────────────────────────────
        try:
            detections = self._object_detector.detect(image_path)
            has_animal = False
            for det in detections:
                if det.is_animal:
                    result.animals.append(det.label)
                    has_animal = True
                else:
                    result.objects.append(det.label)

            # ── Species (BioCLIP) — only when YOLO found an animal ──────────
            if has_animal:
                from backend.database.settings_store import get as _gs
                species = self._species.classify(image_path, threshold=float(_gs('species_threshold')))
                if species and species.label not in result.animals:
                    result.animals.insert(0, species.label)

        except Exception as e:
            logger.warning("Object/animal tagging failed for %s: %s", image_path.name, e)

        # ── Scene / Geography (Places365) ───────────────────────────────────
        try:
            from backend.database.settings_store import get as _gs
            scenes = self._scene_classifier.classify(image_path, top_k=int(_gs('places365_top_k')))
            for s in scenes:
                if s.category == "geography" and s.label not in result.geography:
                    result.geography.append(s.label)
                elif s.category == "place" and s.label not in result.places:
                    result.places.append(s.label)
        except Exception as e:
            logger.warning("Scene classification failed for %s: %s", image_path.name, e)

        # ── Landmarks (CLIP) ────────────────────────────────────────────────
        try:
            from backend.database.settings_store import get as _gs
            landmarks = self._landmark.recognise(image_path, threshold=float(_gs('landmark_threshold')))
            for lm in landmarks:
                if lm.label not in result.places:
                    result.places.append(lm.label)
        except Exception as e:
            logger.warning("Landmark recognition failed for %s: %s", image_path.name, e)

        # ── GPS → Place name (Nominatim) ────────────────────────────────────
        if gps_lat is not None and gps_lon is not None:
            try:
                geo = self._geo.resolve(gps_lat, gps_lon)
                if geo:
                    # Prepend GPS-derived place (highest priority)
                    if geo.label and geo.label not in result.places:
                        result.places.insert(0, geo.label)
            except Exception as e:
                logger.warning("Geo resolution failed: %s", e)

        logger.debug(
            "Tagged %s → objects=%s animals=%s geography=%s places=%s",
            image_path.name, result.objects, result.animals,
            result.geography, result.places,
        )
        return result

    def tag_batch(
        self,
        items: list[tuple[Path, Optional[float], Optional[float]]],
    ) -> list[TagResult]:
        """
        Tag a batch of images, using YOLO batch inference for the GPU-bound
        forward pass, then per-image models for scene / landmark / geo.

        Args:
            items: list of (image_path, gps_lat, gps_lon).

        Returns:
            list[TagResult] in the same order as items.
        """
        results = [TagResult() for _ in items]
        paths = [item[0] for item in items]
        valid_mask = [p.exists() for p in paths]
        valid_paths = [p for p, ok in zip(paths, valid_mask) if ok]

        if not valid_paths:
            return results

        # ── Batch YOLO: one GPU forward pass for all valid images ───────────
        try:
            from backend.database.settings_store import get as _gs
            batch_detections = self._object_detector.detect_batch(valid_paths)
            vi = 0
            for i, ok in enumerate(valid_mask):
                if not ok:
                    continue
                detections = batch_detections[vi]; vi += 1
                has_animal = False
                for det in detections:
                    if det.is_animal:
                        results[i].animals.append(det.label)
                        has_animal = True
                    else:
                        results[i].objects.append(det.label)

                # Species (BioCLIP) — only when YOLO found an animal
                if has_animal:
                    try:
                        species = self._species.classify(
                            paths[i], threshold=float(_gs("species_threshold"))
                        )
                        if species and species.label not in results[i].animals:
                            results[i].animals.insert(0, species.label)
                    except Exception as e:
                        logger.warning("Species classification failed for %s: %s", paths[i].name, e)
        except Exception as e:
            logger.warning("Batch YOLO tagging failed: %s", e)

        # ── Per-image scene / landmark / geo ────────────────────────────────
        for i, (image_path, gps_lat, gps_lon) in enumerate(items):
            if not valid_mask[i]:
                logger.warning("Image not found for tagging: %s", image_path)
                continue
            result = results[i]

            try:
                from backend.database.settings_store import get as _gs
                scenes = self._scene_classifier.classify(
                    image_path, top_k=int(_gs("places365_top_k"))
                )
                for s in scenes:
                    if s.category == "geography" and s.label not in result.geography:
                        result.geography.append(s.label)
                    elif s.category == "place" and s.label not in result.places:
                        result.places.append(s.label)
            except Exception as e:
                logger.warning("Scene classification failed for %s: %s", image_path.name, e)

            try:
                from backend.database.settings_store import get as _gs
                landmarks = self._landmark.recognise(
                    image_path, threshold=float(_gs("landmark_threshold"))
                )
                for lm in landmarks:
                    if lm.label not in result.places:
                        result.places.append(lm.label)
            except Exception as e:
                logger.warning("Landmark recognition failed for %s: %s", image_path.name, e)

            if gps_lat is not None and gps_lon is not None:
                try:
                    geo = self._geo.resolve(gps_lat, gps_lon)
                    if geo and geo.label and geo.label not in result.places:
                        result.places.insert(0, geo.label)
                except Exception as e:
                    logger.warning("Geo resolution failed: %s", e)

            logger.debug(
                "Tagged %s → objects=%s animals=%s geography=%s places=%s",
                image_path.name, result.objects, result.animals,
                result.geography, result.places,
            )

        return results
