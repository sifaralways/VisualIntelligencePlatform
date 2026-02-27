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
            logger.info("Loading YOLOv11s (device=%s) …", self._device)
            # yolo11s.pt auto-downloads to ~/.cache/ultralytics/ on first use
            self._model = YOLO("yolo11s.pt")
            logger.info("✅  YOLOv11s ready")
        except ImportError as e:
            logger.warning("YOLOv11 unavailable — install ultralytics: %s", e)
        except Exception as e:
            logger.error("Failed to load YOLOv11: %s", e)

    def detect(self, image_path: Path, conf_threshold: float = 0.40) -> list[ObjectTag]:
        """
        Detect objects and animals in a JPEG image.

        Returns:
            List of ObjectTag (may be empty). Duplicated class names are
            deduplicated, keeping the highest-confidence instance.
        """
        if self._model is None:
            return []

        try:
            results = self._model(
                str(image_path),
                device=self._device,
                verbose=False,
                conf=conf_threshold,
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
