"""VIP ML — Object and animal detection via YOLOv8 Open Images V7.

Model: Ultralytics YOLOv8n-OIV7 (yolov8n-oiv7.pt, ~6 MB)
       Trained on Open Images V7 — 600 object classes (vs COCO 80).
       Auto-downloads to ~/.cache/ultralytics/ on first use.
Backend: Apple Silicon MPS via PyTorch

Outputs two lists:
  objects  — non-person, non-animal detections  (Car, Television, Laptop …)
  animals  — animal detections                  (Dog, Cat, Bird …)

OIV7 uses Title Case class names (unlike COCO which uses lowercase).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import settings
from backend.database.settings_store import get as get_setting

logger = logging.getLogger(__name__)

# OIV7 classes that are animals — routed to the "animal" category.
# OIV7 uses Title Case (unlike COCO's lowercase).
_ANIMAL_CLASSES = frozenset({
    # Common pets & farm animals
    "Bird", "Cat", "Dog", "Horse", "Sheep", "Cattle", "Goat", "Pig",
    "Rabbit", "Hamster",
    # Wild mammals
    "Elephant", "Bear", "Zebra", "Giraffe", "Lion", "Tiger",
    "Leopard", "Jaguar", "Cheetah", "Fox", "Deer", "Squirrel",
    "Monkey", "Gorilla", "Panda", "Kangaroo", "Koala", "Raccoon",
    "Otter", "Hedgehog", "Mule", "Camel",
    # Aquatic / marine
    "Whale", "Dolphin", "Shark", "Fish", "Seahorse", "Turtle",
    "Crab", "Lobster", "Starfish",
    # Birds (sub-types)
    "Duck", "Owl", "Eagle", "Penguin", "Ostrich", "Parrot",
    # Reptiles & insects
    "Snake", "Lizard", "Crocodile", "Butterfly", "Bee", "Beetle", "Insect",
})

# Classes we don't want in output (detected but uninformative)
# OIV7 uses Title Case.
_SKIP_CLASSES = frozenset({"Person", "Human body", "Human face", "Human hand", "Human leg", "Human arm", "Human foot", "Human hair"})


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
            # Default: yolov8n-oiv7.pt — Open Images V7, 600 classes.
            # Override with VIP_YOLO_MODEL env var.
            self._model = YOLO(settings.yolo_model)

            # Warmup: run one dummy inference so that the predictor is fully
            # initialised (including Conv+BN fusion) before any concurrent
            # detect_batch() calls can race on the unfused model layers.
            # Without this, concurrent Phase-4 threads hit a TOCTOU race in
            # ultralytics' fuse() loop: one thread's hasattr(m, 'bn') returns
            # True just before another thread's delattr(m, 'bn') runs, causing
            # "AttributeError: 'Conv' object has no attribute 'bn'".
            import numpy as np
            _dummy = np.zeros((8, 8, 3), dtype=np.uint8)
            self._model(_dummy, device=self._device, verbose=False)

            logger.info("✅  %s ready (%d classes)", settings.yolo_model, len(self._model.names))
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
