"""
VIP Pipeline — Person centroid utilities.

A persisted centroid on the persons table lets the app recognise known people
in future scans even after their original photos have been removed.

The centroid is a normalised mean of all face embeddings assigned to a person.
It is stored as raw float32 bytes (512-dim) in persons.centroid.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


async def update_person_centroid(db, person_id: int) -> None:
    """
    Recompute and store the normalised centroid for a named person.

    Reads all current embeddings assigned to this person (via faces.person_id),
    averages them into a single unit vector, and writes it to persons.centroid.

    Call this whenever faces are added to or removed from a person:
      - After naming a person / creating a person from cluster
      - After merging a cluster into a person
      - After deleting photos that contained this person's faces

    If the person has no remaining embeddings (all photos deleted), the centroid
    is set to NULL so the person is skipped in matching until new photos arrive.
    """
    rows = await db.execute_fetchall("""
        SELECT e.vector
        FROM embeddings e
        JOIN faces f ON f.id = e.face_id
        WHERE f.person_id = ?
    """, (person_id,))

    if not rows:
        # No live embeddings remain (all photos deleted). Preserve the existing
        # centroid vector — it was computed from real faces and is still valid
        # for re-identification in future scans. Only update the count so the
        # system knows there are currently no live embeddings backing it.
        await db.execute(
            "UPDATE persons SET centroid_n=0 WHERE id=?",
            (person_id,),
        )
        logger.debug(
            "No embeddings for person_id=%d — preserving last centroid for future matching",
            person_id,
        )
        return

    vecs = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
    centroid = vecs.mean(axis=0)
    norm = np.linalg.norm(centroid)
    if norm > 0:
        centroid /= norm

    await db.execute(
        "UPDATE persons SET centroid=?, centroid_n=? WHERE id=?",
        (centroid.tobytes(), len(rows), person_id),
    )
    logger.debug(
        "Updated centroid for person_id=%d from %d embeddings", person_id, len(rows)
    )


def load_centroid(blob: bytes) -> np.ndarray:
    """Deserialise a stored centroid blob to a float32 numpy array."""
    return np.frombuffer(blob, dtype=np.float32).copy()
