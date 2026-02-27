"""
VIP ML — FAISS vector index.

Wraps FAISS flat/IVF index for approximate nearest-neighbour search
across all face embeddings.

Strategy:
  - < 300K vectors → IndexFlatIP (exact, cosine via inner product on normalised vecs)
  - ≥ 300K vectors → IndexIVFFlat (approximate, much faster)
  - Index is persisted to disk at settings.faiss_path
  - Rebuilt from DB when stale (e.g. after new embeddings added)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

from backend.config import settings

logger = logging.getLogger(__name__)


class FaissIndex:
    """Manages the FAISS index lifecycle."""

    def __init__(self) -> None:
        self._index = None
        self._face_ids: list[int] = []   # maps FAISS position → DB face_id

    def build(self, face_ids: list[int], vectors: list[np.ndarray]) -> None:
        """
        (Re)build the index from scratch.

        Args:
            face_ids: DB face IDs (parallel to vectors)
            vectors:  512-D float32 unit vectors
        """
        import faiss

        n = len(vectors)
        if n == 0:
            logger.warning("No vectors to index")
            return

        matrix = np.stack(vectors).astype(np.float32)
        d = matrix.shape[1]   # should be 512

        if n < settings.faiss_use_ivf_above:
            logger.info("Building IndexFlatIP (%d vectors, dim=%d)", n, d)
            self._index = faiss.IndexFlatIP(d)  # inner product = cosine on normalised vecs
        else:
            logger.info("Building IndexIVFFlat (%d vectors, dim=%d, nlist=%d)", n, d, settings.faiss_ivf_nlist)
            quantizer = faiss.IndexFlatIP(d)
            self._index = faiss.IndexIVFFlat(quantizer, d, settings.faiss_ivf_nlist, faiss.METRIC_INNER_PRODUCT)
            self._index.train(matrix)

        self._index.add(matrix)
        self._face_ids = list(face_ids)
        logger.info("FAISS index built: %d vectors", self._index.ntotal)

    def save(self) -> None:
        """Persist index to disk."""
        import faiss
        import pickle

        if self._index is None:
            logger.warning("No index to save")
            return

        faiss.write_index(self._index, str(settings.faiss_path))

        ids_path = settings.faiss_path.with_suffix(".ids.pkl")
        with open(ids_path, "wb") as f:
            import pickle
            pickle.dump(self._face_ids, f)

        logger.info("FAISS index saved to %s", settings.faiss_path)

    def load(self) -> bool:
        """Load index from disk. Returns True if successful."""
        import faiss
        import pickle

        if not settings.faiss_path.exists():
            logger.info("No FAISS index on disk yet")
            return False

        try:
            self._index = faiss.read_index(str(settings.faiss_path))
            ids_path = settings.faiss_path.with_suffix(".ids.pkl")
            with open(ids_path, "rb") as f:
                self._face_ids = pickle.load(f)
            logger.info("FAISS index loaded: %d vectors", self._index.ntotal)
            return True
        except Exception as e:
            logger.error("Failed to load FAISS index: %s", e)
            return False

    def search(
        self,
        query: np.ndarray,
        k: int = 10,
        threshold: float = 0.6,
    ) -> list[tuple[int, float]]:
        """
        Find k nearest face embeddings to a query vector.

        Returns:
            List of (face_id, similarity) sorted by descending similarity.
            Only results with similarity >= threshold are returned.
        """
        if self._index is None or self._index.ntotal == 0:
            return []

        query = query.astype(np.float32).reshape(1, -1)
        similarities, positions = self._index.search(query, k)

        results = []
        for pos, sim in zip(positions[0], similarities[0]):
            if pos < 0 or sim < threshold:
                continue
            if pos < len(self._face_ids):
                results.append((self._face_ids[pos], float(sim)))

        return results

    @property
    def total(self) -> int:
        return self._index.ntotal if self._index else 0
