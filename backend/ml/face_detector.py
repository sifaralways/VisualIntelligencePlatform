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
from backend.database.settings_store import get as get_setting

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

    # ── Additional attributes from Buffalo_L (all optional — depend on model sub-modules) ──
    age: Optional[int] = None             # estimated age in years (GenderAge model)
    gender: Optional[str] = None          # 'Male' | 'Female' (GenderAge model)
    pose_yaw: Optional[float] = None      # head yaw  in degrees
    pose_pitch: Optional[float] = None    # head pitch in degrees
    pose_roll: Optional[float] = None     # head roll  in degrees
    landmarks: Optional[list] = None      # list of {Type, X, Y} following Rekognition naming
    quality_brightness: Optional[float] = None   # mean pixel brightness 0–100
    quality_sharpness: Optional[float] = None    # Laplacian variance → sharpness 0–100
    eyes_open: Optional[bool] = None             # True = eyes appear open (gradient check)


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
            if conf < get_setting('face_detection_threshold'):
                continue

            x1, y1, x2, y2 = face.bbox.astype(int)

            # Clamp to image bounds
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(img_w, x2), min(img_h, y2)

            w, h = x2 - x1, y2 - y1
            min_px = int(get_setting('min_face_size_px'))
            if w < min_px or h < min_px:
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

            # ── Age & Gender (GenderAge sub-model of Buffalo_L) ──────────────
            raw_age    = getattr(face, 'age', None)
            raw_gender = getattr(face, 'gender', None)  # 0=female, 1=male
            age    = int(round(float(raw_age))) if raw_age is not None else None
            gender = ('Male' if raw_gender == 1 else 'Female') if raw_gender is not None else None

            # ── Pose (3D head orientation) ────────────────────────────────────
            pose_yaw = pose_pitch = pose_roll = None
            raw_pose = getattr(face, 'pose', None)
            if raw_pose is not None and len(raw_pose) >= 3:
                # InsightFace convention: [pitch, yaw, roll] in degrees
                pose_pitch = float(raw_pose[0])
                pose_yaw   = float(raw_pose[1])
                pose_roll  = float(raw_pose[2])

            # ── 5-point landmarks (kps) mapped to Rekognition names ───────────
            landmarks = None
            raw_kps = getattr(face, 'kps', None)
            if raw_kps is not None and len(raw_kps) == 5:
                kps_names = ['eyeLeft', 'eyeRight', 'nose', 'mouthLeft', 'mouthRight']
                landmarks = [
                    {'Type': name, 'X': round(float(kp[0]) / img_w, 6),
                                   'Y': round(float(kp[1]) / img_h, 6)}
                    for name, kp in zip(kps_names, raw_kps)
                ]

            # ── Eye-open state (vertical gradient of eye-region patch) ────────
            eyes_open: Optional[bool] = None
            if raw_kps is not None and len(raw_kps) >= 2:
                from backend.ml.quality_checker import check_eyes_open
                eyes_open = check_eyes_open(
                    img,
                    eye_kps_px=[raw_kps[0].tolist(), raw_kps[1].tolist()],
                    face_w_px=w,
                )

            # ── Quality: brightness + sharpness from face crop ────────────────
            quality_brightness = quality_sharpness = None
            if crop.size > 0:
                gray = np.mean(crop, axis=2)                       # luminance approx
                quality_brightness = float(np.clip(np.mean(gray) / 255 * 100, 0, 100))
                # Laplacian variance as sharpness proxy (normalised to 0–100)
                laplacian = np.array([
                    gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:]
                    - 4 * gray[1:-1, 1:-1]
                ])
                lap_var = float(np.var(laplacian))
                quality_sharpness = float(np.clip(lap_var / 500 * 100, 0, 100))

            results.append(DetectedFace(
                bbox_x=x1 / img_w,
                bbox_y=y1 / img_h,
                bbox_w=w / img_w,
                bbox_h=h / img_h,
                detection_conf=conf,
                crop=crop,
                embedding=embedding,
                age=age,
                gender=gender,
                pose_yaw=pose_yaw,
                pose_pitch=pose_pitch,
                pose_roll=pose_roll,
                landmarks=landmarks,
                quality_brightness=quality_brightness,
                quality_sharpness=quality_sharpness,
                eyes_open=eyes_open,
            ))

        return results
