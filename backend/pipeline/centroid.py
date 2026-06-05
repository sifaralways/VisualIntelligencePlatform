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

from backend.database import settings_store
from backend.face_quality import face_quality_score_from_row, select_top_face_rows, weighted_centroid_from_rows

logger = logging.getLogger(__name__)


async def rebuild_person_centroid_faces(
    db,
    person_id: int,
    *,
    prefer_recent_photos: bool = False,
) -> int:
    max_faces = max(1, int(settings_store.get("person_centroid_max_faces") or 100))
    rows = await db.execute_fetchall("""
        SELECT e.face_id,
               e.vector,
               f.detection_conf,
               f.bbox_w,
               f.bbox_h,
               f.face_attributes,
               f.face_sharpness,
               f.pose_yaw,
               f.pose_pitch,
               f.pose_roll,
               mf.date_taken
        FROM embeddings e
        JOIN faces f ON f.id = e.face_id
        JOIN media_files mf ON mf.id = f.media_file_id
        JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
        JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
        JOIN persons p ON p.person_guid = cpc.person_guid
        WHERE p.id = ?
    """, (person_id,))

    await db.execute("DELETE FROM person_centroid_faces WHERE person_id=?", (person_id,))

    if not rows:
        return 0

    selected_rows = select_top_face_rows(
        rows,
        max_faces=max_faces,
        prefer_recent_photos=prefer_recent_photos,
    )
    await db.executemany(
        """
        INSERT INTO person_centroid_faces (person_id, face_id, quality_score)
        VALUES (?, ?, ?)
        """,
        [
            (person_id, int(row["face_id"]), float(face_quality_score_from_row(row)))
            for row in selected_rows
        ],
    )
    return len(selected_rows)


async def update_person_centroid(
    db,
    person_id: int,
    *,
    prefer_recent_photos: bool = False,
) -> None:
    """
    Recompute and store the normalised centroid for a named person.

    Reads all current embeddings assigned to this person through the active
    cluster->person membership graph,
    averages them into a single unit vector, and writes it to persons.centroid.

    Call this whenever faces are added to or removed from a person:
      - After naming a person / creating a person from cluster
      - After merging a cluster into a person
      - After deleting photos that contained this person's faces

    If the person has no remaining embeddings (all photos deleted), the centroid
    is set to NULL so the person is skipped in matching until new photos arrive.
    """
    selected_count = await rebuild_person_centroid_faces(
        db,
        person_id,
        prefer_recent_photos=prefer_recent_photos,
    )

    rows = await db.execute_fetchall("""
        SELECT e.vector,
               f.detection_conf,
               f.bbox_w,
               f.bbox_h,
               f.face_attributes,
               f.face_sharpness,
               f.pose_yaw,
               f.pose_pitch,
             f.pose_roll,
             mf.date_taken
        FROM person_centroid_faces pcf
        JOIN embeddings e ON e.face_id = pcf.face_id
        JOIN faces f ON f.id = pcf.face_id
         JOIN media_files mf ON mf.id = f.media_file_id
        WHERE pcf.person_id = ?
        ORDER BY pcf.quality_score DESC, pcf.face_id DESC
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
        await db.execute("DELETE FROM person_centroid_faces WHERE person_id=?", (person_id,))
        logger.debug(
            "No embeddings for person_id=%d — preserving last centroid for future matching",
            person_id,
        )
        return

    centroid = weighted_centroid_from_rows(
        rows,
        prefer_recent_photos=prefer_recent_photos,
    )
    if centroid is None:
        await db.execute(
            "UPDATE persons SET centroid_n=0 WHERE id=?",
            (person_id,),
        )
        logger.debug(
            "No usable weighted embeddings for person_id=%d — preserving last centroid",
            person_id,
        )
        return

    await db.execute(
        "UPDATE persons SET centroid=?, centroid_n=? WHERE id=?",
        (centroid.tobytes(), selected_count, person_id),
    )
    logger.debug(
        "Updated centroid for person_id=%d from %d exemplar faces (prefer_recent_photos=%s)",
        person_id,
        selected_count,
        prefer_recent_photos,
    )


def load_centroid(blob: bytes) -> np.ndarray:
    """Deserialise a stored centroid blob to a float32 numpy array."""
    return np.frombuffer(blob, dtype=np.float32).copy()
