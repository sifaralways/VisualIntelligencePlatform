"""
VIP ML — Face detection using InsightFace RetinaFace.

Input:  JPEG preview image (extracted from RAW)
Output: List of detected faces with bounding boxes and confidence scores

Model: InsightFace Buffalo_L (RetinaFace detector)
Runtime: InsightFace with CoreML/CPU backend on Apple Silicon
         MLX integration is at the embedding layer (see embedder.py)

Detection is intentionally kept separate from embedding so each can be
batched and profiled independently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from backend.config import settings

logger = logging.getLogger(__name__)


@dataclass
class DetectedFace:
    """A face detected in an image."""
    bbox_x: float           # normalised 0–1
    bbox_y: float
    bbox_w: float
    bbox_h: float
    detection_conf: float   # 0–1 from RetinaFace
    crop: np.ndarray        # RGB uint8 face crop
    embedding: Optional[np.ndarray] = None  # 512-D ArcFace vector, already L2-normalised


class FaceDetector:
    """
    Thin wrapper around InsightFace FaceAnalysis.
    Initialised once and reused across the pipeline.
    """

    def __init__(self) -> None:
        self._app = None

    def load(self) -> None:
        """Load the model. Called once at pipeline start."""
        import insightface
        from insightface.app import FaceAnalysis

        logger.info("Loading InsightFace Buffalo_L...")
        # CoreML EP mishandles the det_10g.onnx dynamic spatial dims at det_size >640:
        # ORT shape inference yields 3200 but CoreML compiles for 1280→12800 → rank mismatch.
        # CPU provider uses Apple Silicon NEON SIMD and is reliable with any det_size.
        self._app = FaceAnalysis(
            name=settings.insightface_model,
            providers=["CPUExecutionProvider"],
        )
        self._app.prepare(ctx_id=0, det_size=(1280, 1280))
        logger.info("✅  Face detector ready")

    def detect(self, image_path: Path) -> list[DetectedFace]:
        """
        Detect faces in a JPEG image file.

        Returns:
            List of DetectedFace (may be empty if no faces found or image unreadable).
        """
        if self._app is None:
            raise RuntimeError("FaceDetector not loaded. Call load() first.")

        try:
            img = np.array(Image.open(image_path).convert("RGB"))
        except Exception as e:
            logger.warning("Cannot open image %s: %s", image_path, e)
            return []

        img_h, img_w = img.shape[:2]

        try:
            faces = self._app.get(img)
        except Exception as e:
            logger.error("Detection error on %s: %s", image_path, e)
            return []

        results = []
        for face in faces:
            conf = float(face.det_score)
            if conf < settings.face_detection_threshold:
                continue

            x1, y1, x2, y2 = face.bbox.astype(int)

            # Clamp to image bounds
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(img_w, x2), min(img_h, y2)

            w, h = x2 - x1, y2 - y1
            if w < settings.min_face_size_px or h < settings.min_face_size_px:
                logger.debug("Skipping tiny face (%dx%d) in %s", w, h, image_path.name)
                continue

            # Add 35% context padding so thumbnails aren't just a tight face box
            pad_x = int(w * 0.35)
            pad_y = int(h * 0.35)
            cx1 = max(0, x1 - pad_x)
            cy1 = max(0, y1 - pad_y)
            cx2 = min(img_w, x2 + pad_x)
            cy2 = min(img_h, y2 + pad_y)
            crop = img[cy1:cy2, cx1:cx2]

            # InsightFace already computed the ArcFace embedding during get().
            # Carry it through so the pipeline doesn't need to re-run inference.
            emb = getattr(face, 'normed_embedding', None)
            embedding = emb.astype(np.float32) if emb is not None else None

            results.append(DetectedFace(
                bbox_x=x1 / img_w,
                bbox_y=y1 / img_h,
                bbox_w=w / img_w,
                bbox_h=h / img_h,
                detection_conf=conf,
                crop=crop,
                embedding=embedding,
            ))

        return results
