"""
VIP ML — ArcFace face embedding via InsightFace.

Input:  Face crops (numpy RGB arrays) from FaceDetector
Output: 512-D float32 unit vectors, one per face

The embedding is the "fingerprint" of a face. Two photos of the same person
produce vectors with cosine similarity > 0.9. Different people produce < 0.6.

Model: InsightFace Buffalo_L (ArcFace w600k_r50 recognition model)
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from backend.config import settings

logger = logging.getLogger(__name__)

MODEL_VERSION = "buffalo_l_v1"  # stored in DB — allows future model migrations


class FaceEmbedder:
    """
    Generates 512-D ArcFace embeddings from face crops.
    Reuses the same InsightFace app instance as FaceDetector for efficiency.
    """

    def __init__(self) -> None:
        self._app = None

    def load(self) -> None:
        """Load InsightFace (or reuse if FaceDetector already loaded it)."""
        import insightface
        from insightface.app import FaceAnalysis

        logger.info("Loading InsightFace Buffalo_L (embedder)...")
        self._app = FaceAnalysis(
            name=settings.insightface_model,
            providers=["CoreMLExecutionProvider", "CPUExecutionProvider"],
        )
        self._app.prepare(ctx_id=0, det_size=(640, 640))
        logger.info("✅  Face embedder ready")

    def embed(self, face_crop: np.ndarray) -> Optional[np.ndarray]:
        """
        Generate 512-D embedding for a single face crop.

        Args:
            face_crop: RGB uint8 numpy array of a face crop.

        Returns:
            512-D float32 unit vector, or None if embedding fails.
        """
        if self._app is None:
            raise RuntimeError("FaceEmbedder not loaded. Call load() first.")

        try:
            # Resize to 112x112 (ArcFace standard input size)
            img = Image.fromarray(face_crop).resize((112, 112))
            img_array = np.array(img)

            faces = self._app.get(img_array)
            if not faces:
                return None

            # Take the face with the highest detection score
            best = max(faces, key=lambda f: f.det_score)
            embedding = best.normed_embedding  # already L2-normalised

            return embedding.astype(np.float32)

        except Exception as e:
            logger.error("Embedding error: %s", e)
            return None

    def embed_batch(self, crops: list[np.ndarray]) -> list[Optional[np.ndarray]]:
        """
        Embed a batch of face crops.
        Returns a list of the same length — None where embedding failed.
        """
        return [self.embed(crop) for crop in crops]

    @staticmethod
    def vector_to_bytes(vector: np.ndarray) -> bytes:
        """Serialise 512-D float32 vector to raw bytes for SQLite BLOB storage."""
        return vector.astype(np.float32).tobytes()

    @staticmethod
    def bytes_to_vector(blob: bytes) -> np.ndarray:
        """Deserialise bytes from SQLite BLOB to numpy array."""
        return np.frombuffer(blob, dtype=np.float32)

    @staticmethod
    def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity between two L2-normalised vectors."""
        return float(np.dot(a, b))

    @property
    def model_version(self) -> str:
        return MODEL_VERSION


def save_face_thumbnail(crop: np.ndarray, face_id: int) -> Path:
    """
    Save a face crop as a small JPEG thumbnail.
    Returns the path.  Directory is created if needed.
    """
    from backend.config import settings

    settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings.thumbnail_dir / f"{face_id}.jpg"

    img = Image.fromarray(crop).resize((200, 200), Image.LANCZOS)
    img.save(out_path, format="JPEG", quality=90)
    return out_path
