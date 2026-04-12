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
from PIL import Image, ImageOps

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
    Wrapper around InsightFace FaceAnalysis supporting three runtime modes:

      0 — Accuracy    : CPUExecutionProvider, det_size=(1280, 1280)
                        Reliable for every face size. ~1.2 s/photo on M-series.
      1 — Performance : CoreMLExecutionProvider (ANE/GPU) + CPU fallback,
                        det_size=(640, 640). Up to 10× faster; may miss faces
                        smaller than ~50 px in the 640-grid.
      2 — Intelligent : Loads both sessions. Uses Signal 1 (EXIF focal length)
                        and Signal 2 (face-size oracle) to pick the cheapest
                        correct path per image automatically.

    CoreML constraint: the det_10g.onnx dynamic spatial dims produce a shape
    mismatch under CoreML when det_size > 640 (ORT expects 12800 anchors;
    CoreML compiles for 3200).  At det_size=(640, 640) both sides agree, so
    CoreML works correctly.  Mode 0 therefore stays on CPU with 1280.
    """

    # Escalation thresholds for Intelligent mode (Signal 2)
    _ESCALATE_MIN_FACES   = 5      # ≥ N faces detected at 640 → escalate
    _ESCALATE_MIN_FACE_W  = 0.04  # any face bbox_w < 4% of frame width → escalate
    # Signal 1 focal-length threshold (mm, 35mm-equivalent)
    _WIDE_ANGLE_MM        = 35

    def __init__(self) -> None:
        self._app_accurate: object | None = None   # CPU, 1280×1280
        self._app_fast: object | None = None       # CoreML, 640×640
        self._loaded_mode: int | None = None

    # ── Model loading ────────────────────────────────────────────────────────

    def load(self) -> None:
        """
        Load or re-prepare model sessions based on the current
        face_detection_mode setting.  Called at the start of every pipeline
        run (after settings cache is refreshed), so a mode change in the
        Admin UI takes effect on the next scan without a server restart.
        """
        import insightface  # noqa: F401 — ensures ORT EP registry is populated
        from insightface.app import FaceAnalysis

        mode = int(get_setting('face_detection_mode'))

        if self._loaded_mode == mode:
            return  # nothing changed — both sessions already in the right state

        logger.info("Preparing face detector for mode %d …", mode)

        # Release stale sessions so memory is freed before allocating fresh ones
        self._app_accurate = None
        self._app_fast = None

        def _make(providers: list[str], det_size: tuple[int, int]) -> object:
            app = FaceAnalysis(name=settings.insightface_model, providers=providers)
            app.prepare(ctx_id=0, det_size=det_size)
            return app

        def _log_providers(app: object, label: str) -> None:
            """
            Log the ORT execution providers that were *actually* activated for
            each sub-model in this FaceAnalysis session.

            InsightFace's FaceAnalysis stores its ONNX sub-models (detection,
            recognition, GenderAge, …) in app.models as a dict of model objects,
            each of which exposes a .session attribute (an ORT InferenceSession).
            Calling session.get_providers() returns the providers ORT actually
            loaded — if CoreML was requested but ORT wasn't built with that EP,
            it silently falls back to CPU and this will show CPUExecutionProvider.
            """
            try:
                models_dict = getattr(app, 'models', {})
                for model_name, model_obj in models_dict.items():
                    session = getattr(model_obj, 'session', None)
                    if session is None:
                        # Some model objects wrap the session one level deeper
                        session = getattr(getattr(model_obj, 'model', None), 'session', None)
                    if session is not None:
                        active = session.get_providers()
                        logger.debug("    [%s] %s → ORT providers in use: %s",
                                     label, model_name, active)
                    else:
                        logger.debug("    [%s] %s — could not find ORT session to introspect",
                                     label, model_name)
            except Exception as exc:
                logger.warning("    [%s] provider introspection failed: %s", label, exc)

        if mode in (0, 2):      # Accuracy or Intelligent need the 1280 CPU session
            logger.debug("  Loading accurate session (CPU, 1280×1280) …")
            self._app_accurate = _make(["CPUExecutionProvider"], (1280, 1280))
            _log_providers(self._app_accurate, "accurate")

        if mode in (1, 2):      # Performance or Intelligent need the 640 CoreML session
            logger.debug("  Loading fast session (CoreML ANE/GPU + CPU fallback, 640×640) …")
            try:
                self._app_fast = _make(
                    ["CoreMLExecutionProvider", "CPUExecutionProvider"], (640, 640)
                )
                _log_providers(self._app_fast, "fast")
            except Exception as _fast_err:
                # CoreML may not be available on this ORT build.  Log clearly and
                # degrade: Performance mode will fall back to the accurate session;
                # Intelligent mode will skip the fast oracle and always use accurate.
                logger.warning(
                    "  ⚠️  Fast (CoreML) session failed to load — will use accurate "
                    "session only. Install onnxruntime-silicon for ANE/GPU support. "
                    "Error: %s", _fast_err
                )
                self._app_fast = None

        self._loaded_mode = mode
        mode_names = {0: "Accuracy", 1: "Performance", 2: "Intelligent"}
        logger.info("✅  Face detector ready — mode: %s", mode_names.get(mode, mode))

    # ── Public entry point ──────────────────────────────────────────────────

    def detect(self, image_path: Path) -> list[DetectedFace]:
        """
        Detect faces in a JPEG image and return rich DetectedFace objects.

        Dispatches to the correct session(s) based on face_detection_mode.
        """
        mode = int(get_setting('face_detection_mode'))

        if mode == 0 and self._app_accurate is None:
            raise RuntimeError("Accurate session not loaded. Call load() first.")
        if mode == 1 and self._app_fast is None and self._app_accurate is None:
            raise RuntimeError("No session loaded. Call load() first.")

        try:
            pil_img = Image.open(image_path)
            # Capture raw EXIF before transpose (used for focal-length signal in
            # Intelligent mode).  The _getexif() call must happen on the original
            # opened image; after exif_transpose the attribute may be missing.
            exif_data = pil_img._getexif() if hasattr(pil_img, '_getexif') else None  # type: ignore[attr-defined]
            # Apply EXIF orientation so the numpy array always has physically upright
            # pixels regardless of how the preview JPEG was generated.  For previews
            # that were already correctly oriented (Orientation=1 or no tag) this is
            # a no-op.  For any preview with a residual orientation tag (e.g. stale
            # cached preview from before the orientation-correction code was added),
            # this ensures the face crop and its saved thumbnail are correctly oriented.
            pil_img = ImageOps.exif_transpose(pil_img)
            img = np.array(pil_img.convert("RGB"))
        except Exception as e:
            logger.warning("Cannot open image %s: %s", image_path, e)
            return []

        img_h, img_w = img.shape[:2]

        if mode == 2:
            return self._detect_intelligent(img, img_w, img_h, image_path, exif_data)
        elif mode == 1:
            # If CoreML failed to load, fall back to accurate session
            session = self._app_fast if self._app_fast is not None else self._app_accurate
            return self._run_session(session, img, img_w, img_h, image_path)
        else:
            return self._run_session(self._app_accurate, img, img_w, img_h, image_path)


    # ── Embedding from pre-loaded array (used by model migration) ───────────

    def embed_from_array(self, img_arr: np.ndarray) -> "np.ndarray | None":
        """
        Extract a 512-D ArcFace embedding from a pre-loaded RGB numpy array.

        Used by run_model_migration() to re-embed named faces from their saved
        200×200 thumbnail JPEGs using the newly-loaded model.  Does not apply
        the min_face_size or detection_threshold filters — the input is already
        a face crop so any detected face is accepted.

        Returns the L2-normalised embedding as float32, or None on failure.
        """
        app = self._app_accurate or self._app_fast
        if app is None:
            logger.error("embed_from_array: no detector session loaded — call load() first")
            return None
        try:
            raw_faces = app.get(img_arr)
            if not raw_faces:
                return None
            best = max(raw_faces, key=lambda f: f.det_score)
            emb = getattr(best, "normed_embedding", None)
            if emb is None:
                return None
            return emb.astype(np.float32)
        except Exception as exc:
            logger.error("embed_from_array failed: %s", exc)
            return None


    # ── Intelligent mode ────────────────────────────────────────────────────

    def _detect_intelligent(
        self,
        img: np.ndarray,
        img_w: int,
        img_h: int,
        image_path: Path,
        exif_data: dict | None = None,
    ) -> list[DetectedFace]:
        """
        Two-signal adaptive detection.

        Signal 1 — EXIF focal length (free, pre-detection):
            Wide-angle (≤ 35 mm) shots are likely to have many small faces
            spread across the frame (events, crowds, landscapes with people).
            Skip the 640 oracle and go straight to the accurate 1280 pass.

        Signal 2 — 640 oracle pass (cheap, ~100–200 ms on ANE):
            Run the fast session first.  If the results suggest the scene is
            complex enough that 1280 would add value, escalate.
            Escalation triggers when:
              • ≥ 5 faces detected (group shot — more small faces likely exist)
              • Any face bbox_w < 4 % of frame width (face near the 640 limit)
            On escalation the 1280 result is returned in full (it subsumes the
            640 result — no merging/deduplication needed).
        """
        # ── Signal 1: focal length ───────────────────────────────────────────
        focal_mm = self._read_focal_length(image_path, exif_data)
        if focal_mm is not None and focal_mm <= self._WIDE_ANGLE_MM:
            logger.debug(
                "Intelligent [%s]: wide-angle (%.0f mm ≤ %d mm) → accurate (1280)",
                image_path.name, focal_mm, self._WIDE_ANGLE_MM,
            )
            return self._run_session(self._app_accurate, img, img_w, img_h, image_path)

        # ── Signal 2: 640 oracle ─────────────────────────────────────────────
        # If CoreML failed to load, skip the oracle and go straight to accurate
        if self._app_fast is None:
            logger.debug("Intelligent [%s]: no fast session — accurate (1280) only", image_path.name)
            return self._run_session(self._app_accurate, img, img_w, img_h, image_path)

        fast_results = self._run_session(self._app_fast, img, img_w, img_h, image_path)

        min_face_w = min((f.bbox_w for f in fast_results), default=1.0)
        should_escalate = (
            len(fast_results) >= self._ESCALATE_MIN_FACES
            or min_face_w < self._ESCALATE_MIN_FACE_W
        )

        if should_escalate:
            crowd   = len(fast_results) >= self._ESCALATE_MIN_FACES
            small   = min_face_w < self._ESCALATE_MIN_FACE_W
            reasons = []
            if crowd:
                reasons.append(f"crowd: {len(fast_results)} faces ≥ {self._ESCALATE_MIN_FACES}")
            if small:
                reasons.append(f"small faces: min_bbox_w={min_face_w:.3f} < {self._ESCALATE_MIN_FACE_W:.2f}")
            logger.debug(
                "Intelligent [%s]: escalating → accurate (1280)  [%s]",
                image_path.name, ", ".join(reasons),
            )
            return self._run_session(self._app_accurate, img, img_w, img_h, image_path)

        logger.debug(
            "Intelligent [%s]: fast (640 CoreML) sufficient  "
            "[faces=%d, min_bbox_w=%.3f]",
            image_path.name, len(fast_results), min_face_w,
        )
        return fast_results

    def _read_focal_length(
        self,
        image_path: Path,
        exif_data: dict | None = None,
    ) -> float | None:
        """
        Read the FocalLength EXIF tag (35mm-equivalent) from a JPEG.

        PIL EXIF tag 37386 = FocalLength (actual lens focal length in mm).
        Tag 41989 = FocalLengthIn35mmFilm — preferred when available since it
        normalises for crop factor, making wide-angle classification consistent
        across full-frame and APS-C cameras.

        Accepts a pre-loaded ``exif_data`` dict (from the same PIL open used to
        decode the image) to avoid opening the file a second time.  Falls back
        to opening the file independently if no dict is supplied.

        Returns None on any failure (missing tag, non-JPEG, corrupt EXIF).
        """
        try:
            if exif_data is None:
                with Image.open(image_path) as pil_img:
                    exif_data = pil_img._getexif()  # type: ignore[attr-defined]
            if not exif_data:
                return None
            # Prefer 35mm-equivalent (tag 41989) for crop-factor normalisation
            fl_35 = exif_data.get(41989)
            if fl_35 is not None:
                return float(fl_35)
            # Fall back to actual focal length (tag 37386)
            fl = exif_data.get(37386)
            if fl is not None:
                return float(fl)
        except Exception:
            pass
        return None

    # ── Core inference ──────────────────────────────────────────────────────

    def _run_session(
        self,
        app: object,
        img: np.ndarray,
        img_w: int,
        img_h: int,
        image_path: Path,
    ) -> list[DetectedFace]:
        """
        Run a single InsightFace FaceAnalysis session on a pre-loaded image
        array and return filtered, richly-attributed DetectedFace objects.
        """
        try:
            raw_faces = app.get(img)  # type: ignore[attr-defined]
        except Exception as e:
            logger.error("Detection error on %s: %s", image_path, e)
            return []

        results = []
        for face in raw_faces:
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

            # InsightFace computes the ArcFace embedding inside get() — carry it through
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
                gray = np.mean(crop, axis=2)
                quality_brightness = float(np.clip(np.mean(gray) / 255 * 100, 0, 100))
                laplacian = np.array([
                    gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:]
                    - 4 * gray[1:-1, 1:-1]
                ])
                lap_var = float(np.var(laplacian))
                quality_sharpness = float(np.clip(lap_var / 500 * 100, 0, 100))

            # ── Sharpness gate — discard bokeh / depth-of-field blurs ─────────
            min_sharpness = float(get_setting('face_min_sharpness'))
            if min_sharpness > 0 and quality_sharpness is not None and quality_sharpness < min_sharpness:
                logger.debug(
                    "Skipping blurry face (sharpness=%.1f < %.1f) in %s",
                    quality_sharpness, min_sharpness, image_path.name,
                )
                continue

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
