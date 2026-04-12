"""
VIP ML — Explicit content detector (NudeNet 3.x).

Uses NudeNet's ONNX-based detector to identify explicit body-part labels.
The detector returns per-region bounding boxes; we collapse them to a
per-photo label list and a single "is explicit" boolean.

Label taxonomy (NudeNet 3.4):
  Explicit (stored as tags, photo marked is_explicit):
    FEMALE_GENITALIA_EXPOSED, MALE_GENITALIA_EXPOSED,
    ANUS_EXPOSED, FEMALE_BREAST_EXPOSED, BUTTOCKS_EXPOSED

  Borderline / covered (stored as tags but not is_explicit):
    FEMALE_GENITALIA_COVERED, FEMALE_BREAST_COVERED,
    ANUS_COVERED, BUTTOCKS_COVERED

  Neutral / face / clothing (not stored):
    FACE_FEMALE, FACE_MALE, BELLY_COVERED, BELLY_EXPOSED,
    FEET_EXPOSED, FEET_COVERED, ARMPITS_COVERED, ARMPITS_EXPOSED,
    MALE_BREAST_EXPOSED

The confidence threshold is read from the settings store at call time
(key: nudenet_confidence_threshold, default 0.5) so it can be tuned
without restarting the server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Labels that constitute "explicit" content.
_EXPLICIT_LABELS: frozenset[str] = frozenset({
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
})

# Labels we want to store as tags (explicit + borderline).
_STORE_LABELS: frozenset[str] = _EXPLICIT_LABELS | frozenset({
    "FEMALE_GENITALIA_COVERED",
    "FEMALE_BREAST_COVERED",
    "ANUS_COVERED",
    "BUTTOCKS_COVERED",
})


@dataclass
class ExplicitResult:
    is_explicit: bool = False
    labels: list[str] = field(default_factory=list)    # deduplicated labels above threshold
    max_score: float = 0.0                              # highest detection score


class ExplicitDetector:
    """
    Wraps NudeNet NudeDetector.  Loaded lazily; silently disabled if
    nudenet is not installed so the pipeline always runs.
    """

    def __init__(self) -> None:
        self._detector = None
        self._available = False

    def load(self) -> None:
        try:
            from nudenet import NudeDetector
            self._detector = NudeDetector()
            self._available = True
            logger.info("ExplicitDetector: NudeNet loaded")
        except ImportError:
            logger.warning("ExplicitDetector: nudenet not installed — explicit detection disabled")
        except Exception as e:
            logger.error("ExplicitDetector: failed to load NudeNet: %s", e)

    @property
    def available(self) -> bool:
        return self._available

    def detect(self, image_path: Path) -> ExplicitResult:
        """
        Run explicit content detection on a single image.
        Returns an empty ExplicitResult if the detector is unavailable.
        """
        if not self._available or self._detector is None:
            return ExplicitResult()

        from backend.database.settings_store import get as _gs
        try:
            threshold = float(_gs("nudenet_confidence_threshold"))
        except Exception:
            threshold = 0.5

        try:
            raw = self._detector.detect(str(image_path))
        except Exception as e:
            logger.warning("ExplicitDetector: detection failed for %s: %s", image_path.name, e)
            return ExplicitResult()

        seen: set[str] = set()
        labels: list[str] = []
        max_score = 0.0
        is_explicit = False

        for det in raw:
            label: str = det.get("class", "")
            score: float = det.get("score", 0.0)

            if score < threshold:
                continue
            if label not in _STORE_LABELS:
                continue

            if score > max_score:
                max_score = score
            if label in _EXPLICIT_LABELS:
                is_explicit = True
            if label not in seen:
                seen.add(label)
                labels.append(label)

        return ExplicitResult(
            is_explicit=is_explicit,
            labels=labels,
            max_score=round(max_score, 4),
        )
