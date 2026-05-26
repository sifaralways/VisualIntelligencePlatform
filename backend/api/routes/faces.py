"""VIP API — Faces routes (thumbnail serving + face management)."""

import json
import math
import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pathlib import Path

from backend.config import settings
from backend.database.db import get_db
from backend.pipeline.centroid import update_person_centroid

router = APIRouter()


async def _requeue_as_singleton(db, face_id: int) -> None:
    """
    Create a new 1-member cluster from the face's own embedding and assign
    the face to it, so the face immediately reappears in the unnamed-faces
    list rather than disappearing until the next pipeline run.

    If the face has no stored embedding (edge case), leaves cluster_id NULL —
    the next clustering phase will handle it.
    """
    emb_row = await (
        await db.execute("SELECT vector FROM embeddings WHERE face_id=?", (face_id,))
    ).fetchone()

    if emb_row and emb_row["vector"]:
        cursor = await db.execute(
            """
            INSERT INTO clusters (centroid, member_count, intra_similarity, is_high_conf)
            VALUES (?, 1, 1.0, 0)
            """,
            (emb_row["vector"],),
        )
        new_cluster_id = cursor.lastrowid
        await db.execute(
            "UPDATE faces SET cluster_id=? WHERE id=?", (new_cluster_id, face_id)
        )


def _min_center_distance_with_orientation_variants(
    face_cx: float,
    face_cy: float,
    region_cx: float,
    region_cy: float,
) -> float:
    """
    Return the minimum centre distance over common orientation transforms.

    VIP detects faces on orientation-corrected previews, while historic XMP
    regions may be stored in a different orientation basis for some RAW files.
    """
    variants = (
        (region_cx, region_cy),
        (1.0 - region_cx, region_cy),
        (region_cx, 1.0 - region_cy),
        (1.0 - region_cx, 1.0 - region_cy),
        (region_cy, 1.0 - region_cx),
        (1.0 - region_cy, region_cx),
    )
    return min(math.hypot(face_cx - x, face_cy - y) for x, y in variants)


def _extract_named_regions_from_history(ext: dict) -> list[dict[str, float | str | None]]:
    named_regions: list[dict[str, float | str | None]] = []
    region_info = ext.get("region_info")
    if isinstance(region_info, dict):
        for r in region_info.get("RegionList", []):
            name = str(r.get("Name") or "").strip()
            area = r.get("Area", {})
            if not name or not isinstance(area, dict) or area.get("Unit") != "normalized":
                continue
            try:
                named_regions.append(
                    {
                        "name": name,
                        "cx": float(area.get("X", 0)),
                        "cy": float(area.get("Y", 0)),
                    }
                )
            except Exception:
                continue

    persons_list = [str(p).strip() for p in (ext.get("persons") or []) if str(p).strip()]
    if not named_regions and len(persons_list) == 1:
        return [{"name": persons_list[0], "cx": None, "cy": None}]
    return named_regions


def _find_best_face_for_region(region: dict[str, float | str | None], unmatched_faces: list[dict]) -> tuple[dict | None, float]:
    if region["cx"] is None or region["cy"] is None:
        return None, float("inf")

    rcx = float(region["cx"])
    rcy = float(region["cy"])
    best_face: dict | None = None
    best_dist = float("inf")
    for f in unmatched_faces:
        if f["bbox_x"] is None:
            continue
        fcx = float(f["bbox_x"]) + float(f["bbox_w"] or 0) / 2.0
        fcy = float(f["bbox_y"]) + float(f["bbox_h"] or 0) / 2.0
        dist = _min_center_distance_with_orientation_variants(fcx, fcy, rcx, rcy)
        if dist < best_dist:
            best_dist = dist
            best_face = f
    return best_face, best_dist


def _single_face_fallback_match(
    named_regions: list[dict[str, float | str | None]],
    unmatched_faces: list[dict],
) -> list[tuple[str, int, int | None]]:
    if len(unmatched_faces) != 1:
        return []

    unique_names = sorted(
        {
            str(r.get("name") or "").strip()
            for r in named_regions
            if str(r.get("name") or "").strip()
        }
    )
    if len(unique_names) != 1:
        return []

    f = unmatched_faces[0]
    return [(unique_names[0], int(f["face_id"]), f["cluster_id"])]


def _match_history_regions_to_faces(
    named_regions: list[dict[str, float | str | None]],
    unmatched_faces: list[dict],
) -> list[tuple[str, int, int | None]]:
    matched: list[tuple[str, int, int | None]] = []
    for region in named_regions:
        if not unmatched_faces:
            break
        if region["cx"] is None or region["cy"] is None:
            if len(unmatched_faces) == 1:
                f = unmatched_faces.pop(0)
                matched.append((str(region["name"]), int(f["face_id"]), f["cluster_id"]))
            continue

        best_face, best_dist = _find_best_face_for_region(region, unmatched_faces)

        if best_face is not None and best_dist <= 0.18:
            matched.append((str(region["name"]), int(best_face["face_id"]), best_face["cluster_id"]))
            unmatched_faces = [f for f in unmatched_faces if f["face_id"] != best_face["face_id"]]

    if matched:
        return matched
    return _single_face_fallback_match(named_regions, unmatched_faces)


async def _get_or_create_person_id(db, person_name: str) -> int:
    existing_person = await (
        await db.execute(
            "SELECT id FROM persons WHERE name=? AND is_merged=0 AND COALESCE(is_ignored, 0)=0 LIMIT 1",
            (person_name,),
        )
    ).fetchone()
    if existing_person:
        return int(existing_person["id"])

    cursor = await db.execute(
        """
        INSERT INTO persons (uuid, name, named_at)
        VALUES (?, ?, datetime('now'))
        """,
        (str(uuid.uuid4()), person_name),
    )
    return int(cursor.lastrowid)


async def _apply_history_matches(
    db,
    media_id: int,
    matched: list[tuple[str, int, int | None]],
) -> None:
    touched_person_ids: set[int] = set()
    for person_name, face_id, cluster_id in matched:
        person_id = await _get_or_create_person_id(db, person_name)
        await db.execute("UPDATE faces SET person_id=? WHERE id=?", (person_id, face_id))
        touched_person_ids.add(person_id)

        if cluster_id is not None:
            await db.execute("UPDATE clusters SET person_id=? WHERE id=?", (person_id, cluster_id))
            await db.execute(
                "UPDATE faces SET person_id=? WHERE cluster_id=? AND person_id IS NULL",
                (person_id, cluster_id),
            )

        await db.execute(
            "INSERT OR REPLACE INTO writeback_queue (media_file_id) VALUES (?)",
            (media_id,),
        )

    for pid in touched_person_ids:
        await update_person_centroid(db, pid)


async def _restore_vip_history_names_for_media(db, media_id: int) -> None:
    """Reconcile unnamed faces for one photo using stored VIP history."""
    media_row = await (
        await db.execute(
            "SELECT external_exif FROM media_files WHERE id=?", (media_id,)
        )
    ).fetchone()
    if not media_row or not media_row["external_exif"]:
        return

    try:
        ext = json.loads(media_row["external_exif"])
    except Exception:
        return

    if not ext.get("identifier"):
        return

    named_regions = _extract_named_regions_from_history(ext)
    if not named_regions:
        return

    face_rows = await db.execute_fetchall(
        """
        SELECT f.id AS face_id, f.cluster_id,
               f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,
               f.person_id
        FROM faces f
        WHERE f.media_file_id = ?
        """,
        (media_id,),
    )
    unmatched_faces = [dict(r) for r in face_rows if r["person_id"] is None]
    if not unmatched_faces:
        return

    matched = _match_history_regions_to_faces(named_regions, unmatched_faces)
    if not matched:
        return

    await _apply_history_matches(db, media_id, matched)


@router.get("/{face_id}/thumbnail")
async def get_face_thumbnail(face_id: int):
    """Serve the face crop thumbnail JPEG."""
    async with get_db() as db:
        row = await (
            await db.execute("SELECT thumbnail_path FROM faces WHERE id=?", (face_id,))
        ).fetchone()

    if not row or not row["thumbnail_path"]:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    raw_path = Path(row["thumbnail_path"])

    # Profile migration moved thumbnails under per-profile directories.
    # Legacy DB rows may still point at the old shared root path.
    path = raw_path if raw_path.exists() else (settings.thumbnail_dir / raw_path.name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file missing on disk")

    # Read into memory so Content-Length comes from actual bytes read, not a
    # potentially stale stat() on a NAS/SMB mount.
    return Response(content=path.read_bytes(), media_type="image/jpeg")


@router.get("/cluster/{cluster_id}")
async def get_cluster_faces(cluster_id: int, limit: int = 20):
    """Return representative face thumbnails for a cluster."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT f.id, f.thumbnail_path, f.detection_conf, f.person_id,
                   mf.file_path, mf.date_taken
            FROM faces f
            JOIN media_files mf ON mf.id = f.media_file_id
            WHERE f.cluster_id = ?
            ORDER BY f.detection_conf DESC
            LIMIT ?
        """, (cluster_id, limit))

    return [dict(r) for r in rows]


@router.get("/media/{media_id}")
async def get_faces_for_media(
    media_id: int,
    include_ignored: bool = Query(False, alias="include_ignored"),
):
    """Return all faces detected in a specific media file, with person names."""
    import json as _json
    async with get_db() as db:
        await _restore_vip_history_names_for_media(db, media_id)

        if include_ignored:
            # Return all faces including those assigned to ignored persons.
            # is_ignored=1 faces are returned with person_name=NULL (they have no real name).
            rows = await db.execute_fetchall("""
                SELECT f.id, f.thumbnail_path, f.detection_conf,
                       f.cluster_id, f.person_id,
                       CASE WHEN p.is_ignored = 0 THEN p.name ELSE NULL END AS person_name,
                       f.face_attributes,
                       COALESCE(p.is_ignored, 0) AS is_ignored
                FROM faces f
                LEFT JOIN persons p ON p.id = f.person_id AND p.is_merged = 0
                WHERE f.media_file_id = ?
                ORDER BY f.detection_conf DESC
            """, (media_id,))
        else:
            rows = await db.execute_fetchall("""
                SELECT f.id, f.thumbnail_path, f.detection_conf,
                       f.cluster_id, f.person_id, p.name AS person_name,
                       f.face_attributes,
                       0 AS is_ignored
                FROM faces f
                LEFT JOIN persons p ON p.id = f.person_id AND p.is_merged = 0 AND p.is_ignored = 0
                WHERE f.media_file_id = ?
                  AND (f.person_id IS NULL OR p.id IS NOT NULL)
                ORDER BY f.detection_conf DESC
            """, (media_id,))
    result = []
    for r in rows:
        d = dict(r)
        sharpness: float | None = None
        if d.get("face_attributes"):
            try:
                attrs = _json.loads(d["face_attributes"])
                raw = attrs.get("Quality", {}).get("Sharpness")
                if raw is not None:
                    sharpness = round(float(raw), 1)
            except Exception:
                pass
        d["sharpness"] = sharpness
        d["is_ignored"] = bool(d["is_ignored"])
        del d["face_attributes"]
        result.append(d)
    return result


@router.delete("/{face_id}/from-cluster")
async def remove_face_from_cluster(face_id: int):
    """Detach a face from its cluster (user flagged it as incorrect).

    The face is immediately placed in a new 1-member cluster so it reappears
    in the unnamed faces list rather than disappearing until the next pipeline.
    """
    async with get_db() as db:
        await db.execute(
            "UPDATE faces SET cluster_id=NULL, person_id=NULL WHERE id=?",
            (face_id,),
        )
        await _requeue_as_singleton(db, face_id)
    return {"status": "removed", "face_id": face_id}


@router.delete("/{face_id}/from-person")
async def remove_face_from_person(face_id: int):
    """
    Remove a face from its person assignment (false positive correction).
    The face is detached from person + cluster so it re-enters the unassigned pool.
    The media file is re-queued for writeback so the person is removed from EXIF.
    """
    async with get_db() as db:
        # Find what person this face belongs to (for writeback re-queue)
        row = await (
            await db.execute(
                "SELECT person_id, media_file_id FROM faces WHERE id=?", (face_id,)
            )
        ).fetchone()

        if not row:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Face not found")

        await db.execute(
            "UPDATE faces SET cluster_id=NULL, person_id=NULL WHERE id=?", (face_id,)
        )

        # Re-queue the media file so writeback rewrites EXIF without this person
        if row["media_file_id"]:
            await db.execute("""
                INSERT OR REPLACE INTO writeback_queue (media_file_id, status, queued_at)
                VALUES (?, 'pending', datetime('now'))
            """, (row["media_file_id"],))

        # Recompute the centroid so it no longer includes this face's embedding.
        # Without this the stored centroid stays stale and future similarity
        # comparisons remain biased toward the ejected face.
        if row["person_id"]:
            await update_person_centroid(db, row["person_id"])

        # Place the face in a new 1-member cluster so it immediately reappears
        # in the unnamed-faces list rather than being lost until re-clustering.
        await _requeue_as_singleton(db, face_id)

    return {"status": "removed", "face_id": face_id}
