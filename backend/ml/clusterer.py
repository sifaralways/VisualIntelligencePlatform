"""
VIP ML — HDBSCAN face clustering.

Input:  All 512-D embeddings from the DB
Output: Cluster assignments per face, cluster quality metrics

Design principles:
  - Cluster stability > speed (HDBSCAN with conservative settings)
  - Embeddings are NEVER deleted — only cluster assignments change
  - High-confidence clusters → single tile in UI
  - Low-confidence clusters  → multi-tile grid review in UI
  - Merge and split are user actions, not automatic corrections
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from backend.config import settings
from backend.ml.embedder import FaceEmbedder

logger = logging.getLogger(__name__)


@dataclass
class ClusterResult:
    """Result of HDBSCAN clustering for a single cluster."""
    label: int                          # HDBSCAN label (-1 = noise/outlier)
    face_ids: list[int]                 # DB face IDs in this cluster
    centroid: np.ndarray                # mean 512-D vector
    intra_similarity: float             # mean pairwise cosine similarity
    is_high_conf: bool                  # above settings.high_confidence_threshold
    member_count: int = field(init=False)

    def __post_init__(self) -> None:
        self.member_count = len(self.face_ids)


def cluster_embeddings(
    face_ids: list[int],
    vectors: list[np.ndarray],
) -> list[ClusterResult]:
    """
    Cluster a set of 512-D face embeddings using HDBSCAN.

    Args:
        face_ids:  DB IDs corresponding to each vector (parallel lists)
        vectors:   512-D float32 unit vectors

    Returns:
        List of ClusterResult, one per cluster (noise/outliers excluded).
        Noise faces (label=-1 from HDBSCAN) are grouped into singleton clusters
        so they still appear in the UI for manual review.
    """
    if not vectors:
        logger.info("No embeddings to cluster")
        return []

    matrix = np.stack(vectors).astype(np.float32)
    logger.info("Clustering %d face embeddings...", len(matrix))

    try:
        from sklearn.cluster import HDBSCAN  # sklearn >= 1.3
        clusterer = HDBSCAN(
            min_cluster_size=settings.hdbscan_min_cluster_size,
            min_samples=settings.hdbscan_min_samples,
            metric="cosine",
            cluster_selection_method="eom",   # excess of mass — stable clusters
            cluster_selection_epsilon=settings.hdbscan_cluster_epsilon,
            # same person across lighting/pose has cosine distance up to ~0.22;
            # epsilon merges sub-clusters within that range so they stay together
        )
    except ImportError:
        import hdbscan
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=settings.hdbscan_min_cluster_size,
            min_samples=settings.hdbscan_min_samples,
            metric="cosine",
            cluster_selection_method="eom",
            cluster_selection_epsilon=settings.hdbscan_cluster_epsilon,
        )

    labels = clusterer.fit_predict(matrix)
    unique_labels = set(labels)
    noise_count = int((labels == -1).sum())
    logger.info("HDBSCAN found %d clusters (noise=%d)", len(unique_labels - {-1}), noise_count)

    results = []

    for label in sorted(unique_labels):
        indices = np.where(labels == label)[0]
        cluster_face_ids = [face_ids[i] for i in indices]
        cluster_vectors = matrix[indices]

        if label == -1:
            # Noise faces: each becomes its own singleton cluster for manual review.
            # Grouping ALL noise into one cluster is wrong — they are different people
            # that HDBSCAN couldn't confidently place, not the same person.
            for fid, vec in zip(cluster_face_ids, cluster_vectors):
                v = vec.copy()
                norm = np.linalg.norm(v)
                if norm > 0:
                    v /= norm
                results.append(ClusterResult(
                    label=-1,
                    face_ids=[fid],
                    centroid=v.astype(np.float32),
                    intra_similarity=1.0,   # singleton: perfect self-similarity
                    is_high_conf=False,     # always show singletons for review
                ))
            continue

        centroid = cluster_vectors.mean(axis=0)
        # Normalise centroid back to unit sphere
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid /= norm

        intra_sim = _mean_cosine_similarity(cluster_vectors)

        # Validity gate: if HDBSCAN grouped faces whose mean pairwise similarity
        # is below the inertia threshold, the cluster is unreliable — different
        # people ended up in the same density region.  Break it into singletons
        # for manual review rather than surfacing it as a single "person".
        if intra_sim < settings.cluster_inertia_threshold and len(cluster_face_ids) > 1:
            logger.info(
                "Cluster label=%d rejected (intra_sim=%.3f < %.3f) — splitting %d faces into singletons",
                label, intra_sim, settings.cluster_inertia_threshold, len(cluster_face_ids),
            )
            for fid, vec in zip(cluster_face_ids, cluster_vectors):
                v = vec.copy()
                norm_v = np.linalg.norm(v)
                if norm_v > 0:
                    v /= norm_v
                results.append(ClusterResult(
                    label=-1,
                    face_ids=[fid],
                    centroid=v.astype(np.float32),
                    intra_similarity=1.0,
                    is_high_conf=False,
                ))
            continue

        results.append(ClusterResult(
            label=label,
            face_ids=cluster_face_ids,
            centroid=centroid.astype(np.float32),
            intra_similarity=intra_sim,
            is_high_conf=intra_sim >= settings.high_confidence_threshold,
        ))

    return results


def _mean_cosine_similarity(vectors: np.ndarray) -> float:
    """
    Mean pairwise cosine similarity within a cluster.
    Vectors are assumed L2-normalised (dot product = cosine similarity).
    Approximated via centroid dot product for speed — good enough.
    """
    if len(vectors) <= 1:
        return 1.0
    centroid = vectors.mean(axis=0)
    norm = np.linalg.norm(centroid)
    if norm == 0:
        return 0.0
    centroid /= norm
    # Mean similarity of all vectors to centroid
    return float(np.dot(vectors, centroid).mean())
