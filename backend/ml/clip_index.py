"""VIP ML - FAISS index for per-photo CLIP embeddings."""

from __future__ import annotations

import logging
import pickle

import numpy as np

from backend.config import settings

logger = logging.getLogger(__name__)


class ClipFaissIndex:
    """Stores CLIP photo embeddings keyed by media_file_id."""

    def __init__(self) -> None:
        self._index = None
        self._media_ids: list[int] = []
        self._dim: int = 0
        self._path: str | None = None

    def _ensure_profile_path(self) -> None:
        path = str(settings.clip_faiss_path)
        if self._path == path:
            return
        self._index = None
        self._media_ids = []
        self._dim = 0
        self._path = path

    def build(self, media_ids: list[int], vectors: list[np.ndarray]) -> None:
        import faiss

        self._ensure_profile_path()

        if not vectors:
            logger.info("ClipFaissIndex: no vectors to build")
            self._index = None
            self._media_ids = []
            self._dim = 0
            return

        matrix = np.stack(vectors).astype(np.float32)
        self._dim = int(matrix.shape[1])

        self._index = faiss.IndexFlatIP(self._dim)
        self._index.add(matrix)
        self._media_ids = list(media_ids)
        logger.info(
            "ClipFaissIndex built: %d vectors (dim=%d)",
            self._index.ntotal,
            self._dim,
        )

    def save(self) -> None:
        import faiss

        self._ensure_profile_path()

        if self._index is None:
            return

        faiss.write_index(self._index, str(settings.clip_faiss_path))
        ids_path = settings.clip_faiss_path.with_suffix(".ids.pkl")
        meta_path = settings.clip_faiss_path.with_suffix(".meta.pkl")

        with open(ids_path, "wb") as f:
            pickle.dump(self._media_ids, f)
        with open(meta_path, "wb") as f:
            pickle.dump({"dim": self._dim}, f)

    def load(self) -> bool:
        import faiss

        self._ensure_profile_path()

        if not settings.clip_faiss_path.exists():
            self._index = None
            self._media_ids = []
            self._dim = 0
            return False

        ids_path = settings.clip_faiss_path.with_suffix(".ids.pkl")
        meta_path = settings.clip_faiss_path.with_suffix(".meta.pkl")

        try:
            self._index = faiss.read_index(str(settings.clip_faiss_path))
            with open(ids_path, "rb") as f:
                self._media_ids = pickle.load(f)
            if meta_path.exists():
                with open(meta_path, "rb") as f:
                    meta = pickle.load(f)
                self._dim = int(meta.get("dim") or 0)
            else:
                self._dim = int(self._index.d)
            return True
        except Exception:
            logger.exception("ClipFaissIndex load failed")
            self._index = None
            self._media_ids = []
            self._dim = 0
            return False

    def search(
        self,
        query_vector: np.ndarray,
        k: int = 50,
        threshold: float = 0.20,
    ) -> list[tuple[int, float]]:
        self._ensure_profile_path()
        if self._index is None or self._index.ntotal == 0:
            return []

        query = query_vector.astype(np.float32).reshape(1, -1)
        if query.shape[1] != self._dim:
            return []

        similarities, positions = self._index.search(query, k)
        out: list[tuple[int, float]] = []
        for pos, sim in zip(positions[0], similarities[0]):
            if pos < 0 or sim < threshold:
                continue
            if pos < len(self._media_ids):
                out.append((self._media_ids[pos], float(sim)))
        return out

    @property
    def total(self) -> int:
        self._ensure_profile_path()
        return self._index.ntotal if self._index is not None else 0

    @property
    def dimension(self) -> int:
        self._ensure_profile_path()
        return self._dim
