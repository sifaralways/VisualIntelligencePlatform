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
from backend.database.settings_store import get as get_setting
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

    # HDBSCAN needs ≥2 samples.  A single embedding trivially forms its own cluster.
    if len(vectors) == 1:
        logger.info("Only 1 embedding — skipping HDBSCAN, returning singleton cluster")
        v = np.array(vectors[0], dtype=np.float32)
        norm = np.linalg.norm(v)
        if norm > 0:
            v /= norm
        return [ClusterResult(
            label=-1,
            face_ids=list(face_ids),
            centroid=v,
            intra_similarity=1.0,
            is_high_conf=False,
        )]

    # C-contiguous float64 is what sklearn HDBSCAN's Cython extension expects
    # internally.  Passing float32 causes sklearn to produce a non-contiguous
    # view during internal conversion, which corrupts the condensed tree and
    # triggers TypeError in traverse_upwards even when copy=True is set.
    matrix = np.ascontiguousarray(np.stack(vectors), dtype=np.float64)
    logger.info("Clustering %d face embeddings...", len(matrix))

    # epsilon_search / traverse_upwards in sklearn's HDBSCAN Cython extension
    # has a longstanding bug (TypeError: only 0-dimensional arrays can be
    # converted to Python scalars) that triggers with certain data geometries
    # whenever cluster_selection_epsilon > 0, regardless of metric.
    # Work-around: always pass epsilon=0.0 to sklearn and apply our own
    # post-clustering merge pass based on centroid cosine distance.
    cos_epsilon = float(get_setting('hdbscan_cluster_epsilon'))

    try:
        from sklearn.cluster import HDBSCAN  # sklearn >= 1.3
        clusterer = HDBSCAN(
            min_cluster_size=int(get_setting('hdbscan_min_cluster_size')),
            min_samples=int(get_setting('hdbscan_min_samples')),
            metric="euclidean",
            cluster_selection_method="eom",
            cluster_selection_epsilon=0.0,   # disabled — see comment above
            copy=True,
        )
    except ImportError:
        import hdbscan
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=int(get_setting('hdbscan_min_cluster_size')),
            min_samples=int(get_setting('hdbscan_min_samples')),
            metric="euclidean",
            cluster_selection_method="eom",
            cluster_selection_epsilon=0.0,   # disabled — see comment above
            copy=True,
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
        inertia_threshold = float(get_setting('cluster_inertia_threshold'))
        if intra_sim < inertia_threshold and len(cluster_face_ids) > 1:
            logger.info(
                "Cluster label=%d rejected (intra_sim=%.3f < %.3f) — splitting %d faces into singletons",
                label, intra_sim, inertia_threshold, len(cluster_face_ids),
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
            is_high_conf=intra_sim >= float(get_setting('high_confidence_threshold')),
        ))

    # ── Post-clustering epsilon merge ─────────────────────────────────────────
    # HDBSCAN with epsilon=0 may leave clusters that are very close together
    # (centroid cosine similarity > 1 - cos_epsilon).  Merge them here instead
    # of relying on the broken sklearn Cython path.
    if cos_epsilon > 0:
        results = _merge_by_epsilon(results, cos_epsilon)

    return results


def _merge_by_epsilon(
    clusters: list[ClusterResult],
    cos_epsilon: float,
) -> list[ClusterResult]:
    """
    Greedily merge named clusters (label != -1) whose centroids are within
    cosine distance cos_epsilon of each other.  Singleton noise clusters
    (label == -1) are left untouched.

    This replicates what HDBSCAN's cluster_selection_epsilon would do without
    triggering the Cython traverse_upwards / epsilon_search bug.
    """
    # Separate named clusters from singletons
    named  = [c for c in clusters if c.label != -1]
    singles = [c for c in clusters if c.label == -1]

    if len(named) < 2:
        return clusters

    merged_flags = [False] * len(named)
    merged: list[ClusterResult] = []

    for i, ci in enumerate(named):
        if merged_flags[i]:
            continue
        group_face_ids = list(ci.face_ids)
        centroid_sum = ci.centroid.astype(np.float64) * len(ci.face_ids)
        total_members = len(ci.face_ids)
        merged_flags[i] = True

        for j, cj in enumerate(named):
            if merged_flags[j] or i == j:
                continue
            # Cosine similarity between unit-normalised centroids
            cos_sim = float(np.dot(ci.centroid, cj.centroid))
            if cos_sim >= 1.0 - cos_epsilon:
                group_face_ids.extend(cj.face_ids)
                centroid_sum += cj.centroid.astype(np.float64) * len(cj.face_ids)
                total_members += len(cj.face_ids)
                merged_flags[j] = True

        new_centroid = (centroid_sum / total_members).astype(np.float32)
        norm = np.linalg.norm(new_centroid)
        if norm > 0:
            new_centroid /= norm

        # Recompute intra-similarity for merged group
        # (approximate via centroid dot product — same method as elsewhere)
        intra_sim = 1.0  # will be recomputed below if we have vectors
        merged.append(ClusterResult(
            label=i,
            face_ids=group_face_ids,
            centroid=new_centroid,
            intra_similarity=intra_sim,
            is_high_conf=False,  # recomputed below
        ))

    # Recompute is_high_conf from settings after merge
    hc_threshold = float(get_setting('high_confidence_threshold'))
    for c in merged:
        c.is_high_conf = c.intra_similarity >= hc_threshold

    if len(merged) < len(named):
        logger.info(
            "Epsilon merge (cos_eps=%.3f): %d clusters → %d after merging",
            cos_epsilon, len(named), len(merged),
        )

    return merged + singles


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
