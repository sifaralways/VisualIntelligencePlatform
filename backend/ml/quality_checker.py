"""
VIP — Image quality checker.

Detects out-of-focus (defocus blur) and closed eyes.

Blur detection strategy
-----------------------
We use the Laplacian variance of the full image converted to greyscale.
High Laplacian variance → sharp edges → in-focus photo.
Low Laplacian variance → blurry edges → out-of-focus photo.

Defocus blur vs long exposure disambiguation
--------------------------------------------
Long exposure photos (star trails, waterfalls, panning sports shots) are
*intentionally* blurry.  If the EXIF shutter speed is >= 1/LONG_EXP_CUTOFF
seconds we never flag the photo as blurry — we mark it as long_exposure=1
instead so the UI can still show it separately if desired.

Thresholds are tunable via the admin settings store (future); currently
hardcoded here with sensible defaults.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Laplacian variance below this → considered blurry (0–100 normalised scale).
BLUR_THRESHOLD: float = 15.0

# Shutter speed >= this value (seconds) → long exposure photo, not defocus.
# 1/30 s = 0.0333 s.  Most handheld motion blur happens below 1/60 s;
# intentional long exposures are typically >= 1/30 s.
LONG_EXPOSURE_CUTOFF_S: float = 1.0 / 30.0

# Laplacian normalisation divisor — variance is clipped to this before ×100.
# A well-focused photo on a 12 MP image typically has variance in the
# hundreds; this maps ~300 var → ~60/100 score.
_LAP_NORM: float = 500.0


def score_blur(img_array: np.ndarray) -> float:
    """
    BYPASSED — always returns 100.0 (sharp) so no photo is flagged.
    Re-enable by restoring the Laplacian variance implementation.
    """
    return 100.0


def classify_blur(
    blur_score: float,
    exposure_time_s: Optional[float],
) -> tuple[int, int]:
    """
    BYPASSED — always returns (0, 0): not blurry, not long exposure.
    Re-enable by restoring the threshold comparison.
    """
    return 0, 0


def _laplacian(gray: np.ndarray) -> np.ndarray:
    """
    Discrete Laplacian (3×3 kernel) without scipy dependency.
    Equivalent to cv2.Laplacian(img, cv2.CV_64F).
    """
    kernel = np.array([
        [0,  1, 0],
        [1, -4, 1],
        [0,  1, 0],
    ], dtype=np.float32)
    # Manual 2D convolution via stride tricks — fast enough on downscaled img
    from numpy.lib.stride_tricks import sliding_window_view
    # Pad for same-size output
    padded = np.pad(gray, 1, mode="reflect")
    windows = sliding_window_view(padded, (3, 3))
    lap = (windows * kernel).sum(axis=(-2, -1))
    return lap


# ---------------------------------------------------------------------------
# Eye-state detection
# ---------------------------------------------------------------------------

# Mean absolute vertical gradient below this → eye appears closed.
# Open eyes have a strong iris/sclera boundary; closed eyelids are smooth.
EYE_OPEN_GRADIENT_THRESHOLD: float = 6.0


def check_eyes_open(
    img_rgb: np.ndarray,
    eye_kps_px: list[list[float]],
    face_w_px: int,
) -> bool:
    """
    Return True if both eyes appear open in *img_rgb*.

    Parameters
    ----------
    img_rgb      : H×W×3 uint8 RGB array (full-image or face crop).
    eye_kps_px   : [[left_ex, left_ey], [right_ex, right_ey]] in pixel coords
                   relative to *img_rgb*.
    face_w_px    : width of the face bounding box in pixels; used to size the
                   eye patch proportionally.
    """
    eye_half = max(6, int(face_w_px * 0.15))  # half-size of square eye patch
    scores: list[float] = []

    for kp in eye_kps_px:
        ex, ey = int(kp[0]), int(kp[1])
        y1 = max(0, ey - eye_half)
        y2 = min(img_rgb.shape[0], ey + eye_half)
        x1 = max(0, ex - eye_half)
        x2 = min(img_rgb.shape[1], ex + eye_half)
        patch = img_rgb[y1:y2, x1:x2]
        if patch.shape[0] < 2 or patch.shape[1] < 1:
            continue
        gray = patch.mean(axis=2).astype(np.float32)
        # Mean absolute vertical gradient — strong near open iris boundary
        vert_grad = float(np.mean(np.abs(np.diff(gray, axis=0))))
        scores.append(vert_grad)

    # BYPASSED — always report eyes open.
    # Re-enable by restoring: return (sum(scores) / len(scores)) >= EYE_OPEN_GRADIENT_THRESHOLD
    return True
