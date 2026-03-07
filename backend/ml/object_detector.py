"""
VIP ML — Object and animal detection via YOLOv11.

Model: Ultralytics YOLOv11 small (yolo11s.pt, ~21MB)
       Trained on COCO 80 classes
Backend: Apple Silicon MPS via PyTorch

Outputs two lists:
  objects  — non-person, non-animal detections  (Car, TV, Laptop …)
  animals  — animal detections                  (Dog, Cat, Bird …)

COCO animal class names (indices 14–23):
  bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import settings
from backend.database.settings_store import get as get_setting

logger = logging.getLogger(__name__)

# COCO classes that are animals — routed to the "animal" category
_ANIMAL_CLASSES = frozenset({
    "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe",
})

# Classes we don't want in output (detected but uninformative)
_SKIP_CLASSES = frozenset({"person"})


@dataclass
class ObjectTag:
    label: str
    confidence: float
    is_animal: bool


class ObjectDetector:
    """YOLOv11-based object and animal detector."""

    def __init__(self) -> None:
        self._model = None
        self._device: str = "cpu"

    def load(self) -> None:
        """Lazy-load YOLOv11. Safe to call multiple times."""
        if self._model is not None:
            return

        try:
            import torch
            from ultralytics import YOLO

            self._device = "mps" if torch.backends.mps.is_available() else "cpu"
            logger.info("Loading %s (device=%s) …", settings.yolo_model, self._device)
            # Model auto-downloads to ~/.cache/ultralytics/ on first use.
            # Default: yolo11m.pt — medium model balances accuracy vs speed.
            # Change VIP_YOLO_MODEL=yolo11l.pt for highest accuracy.
            self._model = YOLO(settings.yolo_model)
            logger.info("✅  %s ready", settings.yolo_model)
        except ImportError as e:
            logger.warning("YOLOv11 unavailable — install ultralytics: %s", e)
        except Exception as e:
            logger.error("Failed to load YOLOv11: %s", e)

    def detect(self, image_path: Path, conf_threshold: float | None = None) -> list[ObjectTag]:
        """
        Detect objects and animals in a JPEG image.

        Returns:
            List of ObjectTag (may be empty). Duplicated class names are
            deduplicated, keeping the highest-confidence instance.
        """
        if self._model is None:
            return []

        threshold = conf_threshold if conf_threshold is not None else get_setting('yolo_conf_threshold')
        try:
            results = self._model(
                str(image_path),
                device=self._device,
                verbose=False,
                conf=threshold,
            )
        except Exception as e:
            logger.warning("YOLOv11 detection failed on %s: %s", image_path.name, e)
            return []

        # Deduplicate: keep highest-confidence per class
        best: dict[str, ObjectTag] = {}
        for result in results:
            boxes = result.boxes
            if boxes is None:
                continue
            for cls_idx, conf in zip(
                boxes.cls.tolist(), boxes.conf.tolist()
            ):
                name = result.names[int(cls_idx)]
                if name in _SKIP_CLASSES:
                    continue
                if name not in best or conf > best[name].confidence:
                    best[name] = ObjectTag(
                        label=name.replace("-", " ").title(),
                        confidence=float(conf),
                        is_animal=name in _ANIMAL_CLASSES,
                    )

        return sorted(best.values(), key=lambda t: t.confidence, reverse=True)

    def detect_batch(
        self,
        image_paths: list[Path],
        conf_threshold: float | None = None,
    ) -> list[list[ObjectTag]]:
        """
        Detect objects/animals for a batch of images in one YOLO forward pass.

        Returns one list[ObjectTag] per image, in the same order as
        image_paths.  Paths that don't exist are returned as [].
        Using a list of paths triggers Ultralytics' native batch mode,
        which is significantly faster than N individual calls on MPS.
        """
        if self._model is None or not image_paths:
            return [[] for _ in image_paths]

        threshold = (
            conf_threshold
            if conf_threshold is not None
            else get_setting("yolo_conf_threshold")
        )
        try:
            yolo_results = self._model(
                [str(p) for p in image_paths],
                device=self._device,
                verbose=False,
                conf=threshold,
            )
        except Exception as e:
            logger.warning("YOLOv11 batch detection failed: %s", e)
            return [[] for _ in image_paths]

        output: list[list[ObjectTag]] = []
        for result in yolo_results:
            best: dict[str, ObjectTag] = {}
            boxes = result.boxes
            if boxes is not None:
                for cls_idx, conf in zip(boxes.cls.tolist(), boxes.conf.tolist()):
                    name = result.names[int(cls_idx)]
                    if name in _SKIP_CLASSES:
                        continue
                    if name not in best or conf > best[name].confidence:
                        best[name] = ObjectTag(
                            label=name.replace("-", " ").title(),
                            confidence=float(conf),
                            is_animal=name in _ANIMAL_CLASSES,
                        )
            output.append(sorted(best.values(), key=lambda t: t.confidence, reverse=True))
        return output
