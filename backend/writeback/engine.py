"""
VIP Writeback — dry-run / confirm / execute engine.

Flow:
  1. User names people in UI  →  DB updated, writeback_queue populated
  2. User opens /writeback page  →  GET /api/writeback/preview  (dry-run)
  3. User reviews changes  →  POST /api/writeback/confirm
  4. Engine writes via ExifTool, updates writeback_queue status
  5. User can purge backups via UI once satisfied
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from backend.config import settings
from backend.database.db import get_db
from backend.ml.analysis_builder import merge_analysis_document, build_hierarchical_subjects
from backend.writeback.exiftool import ExifToolWriter
from backend.writeback.fields import build_field_map, FaceRegion, VIP_SUBJECT_PREFIXES

logger = logging.getLogger(__name__)

_writer = ExifToolWriter()


def _merge_with_existing_xmp(vip_fields: dict, existing: dict) -> dict:
    """
    Merge VIP-generated fields with the metadata already present in the file,
    so that repeated writebacks and edits from other apps are never destroyed.

    Rules applied per field:
    - XMP:PersonInImage  — union(VIP persons, existing file persons).
                           VIP is the authority for its own persons; external
                           persons (added by Lightroom, Photos, etc.) are kept.
    - XMP:Subject /
      IPTC:Keywords      — VIP-prefixed keywords (obj:, animal:, geo:, place:)
                           are replaced with the current DB values.
                           All other existing keywords (Lightroom tags, custom
                           keywords) are preserved and merged in.
    - All other fields   — VIP value wins (GPS, XMP:Location, XMP:Identifier,
                           XMP-mwg-rs:Regions).
    """
    merged = dict(vip_fields)

    # ── PersonInImage: union — never remove a known person from the file ────
    file_persons = existing.get("XMP:PersonInImage", [])
    if isinstance(file_persons, str):
        file_persons = [file_persons]
    vip_persons = list(vip_fields.get("XMP:PersonInImage", []))
    if file_persons:
        # Preserve order: VIP persons first, then file-only persons appended.
        vip_set = set(vip_persons)
        extra = [p for p in file_persons if p not in vip_set]
        merged["XMP:PersonInImage"] = vip_persons + extra

    # ── Subject / IPTC:Keywords: keep external keywords, refresh VIP ones ───
    file_subject = existing.get("XMP:Subject", [])
    if isinstance(file_subject, str):
        file_subject = [file_subject]
    vip_subject = list(vip_fields.get("XMP:Subject", []))
    if file_subject:
        # External keywords: anything without a VIP prefix (Lightroom tags,
        # event names, custom labels, person names from other apps, etc.)
        external = [kw for kw in file_subject
                    if not any(kw.startswith(p) for p in VIP_SUBJECT_PREFIXES)]
        # Merged = VIP keywords (cleared+set, so current) + external (preserved)
        vip_kw_set = set(vip_subject)
        extra_kw = [kw for kw in external if kw not in vip_kw_set]
        merged_kw = vip_subject + extra_kw
        if merged_kw:
            merged["XMP:Subject"]    = merged_kw
            merged["IPTC:Keywords"]  = merged_kw

    return merged


async def preview_pending() -> list[dict]:
    """
    Return a dry-run preview of all pending writes.
    Nothing is written. Returns list of preview dicts.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT wq.id, wq.media_file_id, mf.file_path, mf.writeback_done
            FROM writeback_queue wq
            JOIN media_files mf ON mf.id = wq.media_file_id
            WHERE wq.status = 'pending'
        """)

    if not rows:
        return []

    media_ids = [r["media_file_id"] for r in rows]
    all_fields = await _build_fields_batch(media_ids)

    return [
        {
            "queue_id": row["id"],
            "media_file_id": row["media_file_id"],
            "file_path": row["file_path"],
            "fields": all_fields.get(row["media_file_id"], {}),
        }
        for row in rows
    ]


async def execute_writes(queue_ids: list[int] | None = None) -> dict:
    """
    Execute ExifTool writes for confirmed queue items.

    Uses ExifTool's -stay_open persistent process (one Perl startup for the
    whole batch) and batches all DB reads + writes to minimise round-trips.

    Args:
        queue_ids: Specific queue IDs to write, or None for all pending.

    Returns:
        Summary dict with written/failed counts.
    """
    async with get_db() as db:
        if queue_ids:
            placeholders = ",".join("?" * len(queue_ids))
            rows = await db.execute_fetchall(
                f"SELECT wq.id, wq.media_file_id, mf.file_path, mf.writeback_done "
                f"FROM writeback_queue wq JOIN media_files mf ON mf.id=wq.media_file_id "
                f"WHERE wq.id IN ({placeholders}) AND wq.status='pending'",
                queue_ids,
            )
        else:
            rows = await db.execute_fetchall("""
                SELECT wq.id, wq.media_file_id, mf.file_path, mf.writeback_done
                FROM writeback_queue wq
                JOIN media_files mf ON mf.id = wq.media_file_id
                WHERE wq.status = 'pending'
            """)

    if not rows:
        return {"written": 0, "failed": 0}

    # Build all field maps in 4 bulk queries instead of 4×N.
    media_ids = [r["media_file_id"] for r in rows]
    all_fields = await _build_fields_batch(media_ids)

    # Run all ExifTool writes in a single executor thread using the persistent
    # stay_open process — eliminates one Perl interpreter startup per file.
    loop = asyncio.get_event_loop()

    def _do_writes() -> list[tuple[int, int, bool, str]]:
        """Returns list of (queue_id, media_id, success, msg)."""
        # Batch-read existing PersonInImage + Subject from all target files
        # in one ExifTool subprocess so external metadata is never wiped.
        file_paths = [Path(r["file_path"]) for r in rows]
        existing_xmp = ExifToolWriter.read_xmp_fields(
            [p for p in file_paths if p.exists()]
        )

        writer = ExifToolWriter()
        writer.open()
        results: list[tuple[int, int, bool, str]] = []
        try:
            for row in rows:
                queue_id = row["id"]
                media_id = row["media_file_id"]
                file_path = Path(row["file_path"])
                is_first_write = not bool(row["writeback_done"])
                vip_fields = all_fields.get(media_id, {})

                if not vip_fields:
                    logger.info("No fields to write for media_id=%d, skipping", media_id)
                    results.append((queue_id, media_id, True, "skipped"))
                    continue

                # Merge VIP fields with whatever is already in the file so
                # persons and keywords from other apps are preserved.
                existing = existing_xmp.get(str(file_path), {})
                fields = _merge_with_existing_xmp(vip_fields, existing)

                success, msg = writer.write(
                    file_path, fields, dry_run=False, is_first_write=is_first_write
                )
                logger.info(
                    "Write %s for %s: %s",
                    "OK" if success else "FAILED", file_path.name, msg,
                )
                results.append((queue_id, media_id, success, msg))
        finally:
            writer.close()
        return results

    write_results = await loop.run_in_executor(None, _do_writes)

    # Batch all DB status updates in one transaction.
    queue_written: list[tuple] = []
    media_written: list[tuple] = []
    queue_failed:  list[tuple] = []

    for queue_id, media_id, success, msg in write_results:
        if msg == "skipped":
            continue
        if success:
            queue_written.append((queue_id,))
            media_written.append((media_id,))
        else:
            queue_failed.append((msg, queue_id))

    async with get_db() as db:
        if queue_written:
            await db.executemany(
                "UPDATE writeback_queue SET status='written', written_at=datetime('now') WHERE id=?",
                queue_written,
            )
            await db.executemany(
                "UPDATE media_files SET writeback_done=1, writeback_at=datetime('now') WHERE id=?",
                media_written,
            )
        if queue_failed:
            await db.executemany(
                "UPDATE writeback_queue SET status='failed', error_msg=? WHERE id=?",
                queue_failed,
            )

    return {"written": len(queue_written), "failed": len(queue_failed)}


async def _build_fields_batch(media_ids: list[int]) -> dict[int, dict]:
    """
    Build XMP field maps for multiple media files using 4 bulk DB queries
    instead of 4 queries × N files.

    Persons, GPS/vip_id, tags, and stored analysis docs are all loaded in one
    round-trip each.  Hierarchical subjects are derived from the stored model
    document (not the fully-merged live document), so user label amendments are
    not reflected — person names and all tags are always up-to-date.

    Returns: {media_file_id: field_dict}  (empty dict = nothing to write)
    """
    if not media_ids:
        return {}

    ph = ",".join("?" * len(media_ids))
    async with get_db() as db:
        person_rows = await db.execute_fetchall(f"""
            SELECT DISTINCT f.media_file_id, p.name, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h
            FROM faces f
            JOIN persons p ON p.id = f.person_id
            WHERE f.media_file_id IN ({ph})
              AND p.name IS NOT NULL
              AND p.is_merged = 0
        """, media_ids)

        meta_rows = await db.execute_fetchall(f"""
            SELECT id, gps_lat, gps_lon, vip_id
            FROM media_files WHERE id IN ({ph})
        """, media_ids)

        tag_rows = await db.execute_fetchall(f"""
            SELECT media_file_id, category, label
            FROM media_tags
            WHERE media_file_id IN ({ph})
            ORDER BY category, rowid
        """, media_ids)

        analysis_rows = await db.execute_fetchall(f"""
            SELECT media_file_id, model_document
            FROM photo_analysis
            WHERE media_file_id IN ({ph})
        """, media_ids)

    # Group by media_id
    persons_by_mid: dict[int, list] = {}
    for r in person_rows:
        persons_by_mid.setdefault(r["media_file_id"], []).append(r)

    meta_by_mid: dict[int, dict] = {r["id"]: r for r in meta_rows}

    tags_by_mid: dict[int, list] = {}
    for r in tag_rows:
        tags_by_mid.setdefault(r["media_file_id"], []).append(r)

    docs_by_mid: dict[int, dict] = {}
    for r in analysis_rows:
        try:
            docs_by_mid[r["media_file_id"]] = json.loads(r["model_document"])
        except Exception:
            pass

    result: dict[int, dict] = {}
    for media_id in media_ids:
        person_list = persons_by_mid.get(media_id, [])
        tag_list    = tags_by_mid.get(media_id, [])

        if not person_list and not tag_list:
            result[media_id] = {}
            continue

        # Deduplicate person names, preserving first-seen order.
        seen: set[str] = set()
        names: list[str] = []
        for r in person_list:
            if r["name"] not in seen:
                seen.add(r["name"])
                names.append(r["name"])

        regions = [
            FaceRegion(
                name=r["name"],
                x=r["bbox_x"] + r["bbox_w"] / 2,
                y=r["bbox_y"] + r["bbox_h"] / 2,
                w=r["bbox_w"],
                h=r["bbox_h"],
            )
            for r in person_list
            if r["bbox_x"] is not None
        ]

        meta    = meta_by_mid.get(media_id, {})
        gps_lat = meta.get("gps_lat")
        gps_lon = meta.get("gps_lon")
        vip_id  = meta.get("vip_id")

        objects: list[str]   = []
        animals: list[str]   = []
        geography: list[str] = []
        places: list[str]    = []
        for t in tag_list:
            cat, label = t["category"], t["label"]
            if cat == "object":       objects.append(label)
            elif cat == "animal":     animals.append(label)
            elif cat == "geography":  geography.append(label)
            elif cat == "place":      places.append(label)

        hierarchical_subjects: list[str] | None = None
        doc = docs_by_mid.get(media_id, {})
        if doc:
            hs = build_hierarchical_subjects(doc.get("Labels", []))
            hierarchical_subjects = hs or None

        result[media_id] = build_field_map(
            person_names=names or None,
            face_regions=regions or None,
            objects=objects or None,
            animals=animals or None,
            geography=geography or None,
            places=places or None,
            gps_lat=gps_lat,
            gps_lon=gps_lon,
            vip_id=vip_id,
            hierarchical_subjects=hierarchical_subjects,
        )

    return result


async def _build_fields_for_file(media_file_id: int) -> dict:
    """Assemble the complete XMP field map for a given media file.

    Uses the *effective* analysis document (model doc + user amendments +
    resolved person names) so that EXIF always reflects the curator's intent,
    not the raw model output.
    """
    async with get_db() as db:
        # Named persons with bounding boxes
        person_rows = await db.execute_fetchall("""
            SELECT DISTINCT p.name, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h
            FROM faces f
            JOIN persons p ON p.id = f.person_id
            WHERE f.media_file_id = ?
              AND p.name IS NOT NULL
              AND p.is_merged = 0
        """, (media_file_id,))

        # GPS + vip_id from media_files
        meta_row = await (
            await db.execute(
                "SELECT gps_lat, gps_lon, vip_id FROM media_files WHERE id=?", (media_file_id,)
            )
        ).fetchone()

        # ML-generated tags
        tag_rows = await db.execute_fetchall("""
            SELECT category, label FROM media_tags
            WHERE media_file_id = ?
            ORDER BY category, rowid
        """, (media_file_id,))

        # Effective analysis document (model doc + amendments + resolved person names)
        effective_doc = await merge_analysis_document(media_file_id, db)

    if not person_rows and not tag_rows:
        return {}

    # Deduplicate person names
    seen: set[str] = set()
    names: list[str] = []
    for r in person_rows:
        if r["name"] not in seen:
            seen.add(r["name"])
            names.append(r["name"])

    regions = [
        FaceRegion(
            name=r["name"],
            x=r["bbox_x"] + r["bbox_w"] / 2,
            y=r["bbox_y"] + r["bbox_h"] / 2,
            w=r["bbox_w"],
            h=r["bbox_h"],
        )
        for r in person_rows
        if r["bbox_x"] is not None
    ]

    gps_lat = meta_row["gps_lat"] if meta_row else None
    gps_lon = meta_row["gps_lon"] if meta_row else None
    vip_id  = meta_row["vip_id"]  if meta_row else None

    # Group tags by category
    objects: list[str] = []
    animals: list[str] = []
    geography: list[str] = []
    places: list[str] = []
    for t in tag_rows:
        cat, label = t["category"], t["label"]
        if cat == "object":
            objects.append(label)
        elif cat == "animal":
            animals.append(label)
        elif cat == "geography":
            geography.append(label)
        elif cat == "place":
            places.append(label)

    # Build hierarchical subjects from effective document labels
    hierarchical_subjects: list[str] | None = None
    if effective_doc:
        hs = build_hierarchical_subjects(effective_doc.get("Labels", []))
        hierarchical_subjects = hs or None

    return build_field_map(
        person_names=names or None,
        face_regions=regions or None,
        objects=objects or None,
        animals=animals or None,
        geography=geography or None,
        places=places or None,
        gps_lat=gps_lat,
        gps_lon=gps_lon,
        vip_id=vip_id,
        hierarchical_subjects=hierarchical_subjects,
    )


async def write_single_file(media_file_id: int) -> dict:
    """
    Write EXIF metadata for *one* file immediately — no writeback_queue entry needed.

    This is the engine behind the per-photo "Write to EXIF" button in the UI.
    The file must be present on local disk (not an iCloud stub).

    Returns:
        {"status": "written",  "fields_written": [...sorted field names...]}
        {"status": "skipped",  "reason": "No metadata to write"}

    Raises:
        ValueError   — media_file_id not found, file is a stub, or not on disk
        RuntimeError — ExifTool write failure
    """
    async with get_db() as db:
        row = await (await db.execute(
            "SELECT id, file_path, is_stub, writeback_done FROM media_files WHERE id=?",
            (media_file_id,)
        )).fetchone()

    if row is None:
        raise ValueError(f"Media file {media_file_id} not found")
    if row["is_stub"]:
        raise ValueError("File is an iCloud stub — download it first then retry")

    file_path = Path(row["file_path"])
    if not file_path.exists():
        raise ValueError(f"File not found on disk: {file_path.name}")

    fields = await _build_fields_for_file(media_file_id)
    if not fields:
        return {"status": "skipped", "reason": "No metadata to write yet", "fields_written": []}

    # Merge with what's already in the file so existing persons/keywords survive.
    loop = asyncio.get_event_loop()
    existing_xmp = await loop.run_in_executor(
        None, ExifToolWriter.read_xmp_fields, [file_path]
    )
    existing = existing_xmp.get(str(file_path), {})
    fields = _merge_with_existing_xmp(fields, existing)

    is_first_write = not bool(row["writeback_done"])
    success, msg = _writer.write(file_path, fields, dry_run=False, is_first_write=is_first_write)

    if not success:
        raise RuntimeError(f"ExifTool write failed: {msg}")

    async with get_db() as db:
        await db.execute(
            "UPDATE media_files SET writeback_done=1, writeback_at=datetime('now') WHERE id=?",
            (media_file_id,)
        )

    logger.info("Single write OK for media_id=%d — %d fields", media_file_id, len(fields))
    return {"status": "written", "fields_written": sorted(fields.keys())}
