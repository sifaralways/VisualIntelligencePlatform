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
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from backend.config import settings
from backend.database.db import get_db
from backend.ml.analysis_builder import merge_analysis_document, build_hierarchical_subjects
from backend.writeback.exiftool import ExifToolWriter
from backend.writeback.fields import build_field_map, FaceRegion, VIP_SUBJECT_PREFIXES

logger = logging.getLogger(__name__)

_writer = ExifToolWriter()


async def _load_remote_servers() -> list[dict]:
    """Return all enabled remote server configs from the DB."""
    try:
        async with get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM remote_servers WHERE enabled=1 ORDER BY id"
            )
        return [dict(r) for r in rows]
    except Exception as exc:
        # Table may not exist yet on first run before migration 014.
        logger.debug("_load_remote_servers: %s", exc)
        return []


def _match_remote_server(
    file_path_str: str, servers: list[dict]
) -> dict | None:
    """
    Return the first enabled remote server whose local_path_prefix matches
    the given file path, or None for local-only dispatch.
    """
    for srv in servers:
        prefix = srv.get("local_path_prefix", "")
        if prefix and file_path_str.startswith(prefix):
            return srv
    return None


def _translate_path(file_path_str: str, server: dict) -> str:
    """Translate a local mount path to the server-side path."""
    local_prefix  = server["local_path_prefix"]
    remote_prefix = server["remote_path_prefix"]
    return remote_prefix + file_path_str[len(local_prefix):]


def _try_icloud_download(file_path: Path, timeout_sec: int = 60) -> bool:
    """
    Trigger iCloud download for an evicted stub file and wait for it to
    become locally available.

    Uses the macOS ``brctl download`` command to request the file from iCloud,
    then polls ``Path.exists()`` every 2 seconds until the file appears or
    *timeout_sec* is exceeded.

    Returns True when the file is accessible, False on timeout.
    """
    try:
        subprocess.run(
            ["brctl", "download", str(file_path)],
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        logger.debug("brctl download request failed for %s: %s", file_path.name, exc)

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if file_path.exists():
            return True
        time.sleep(2)
    return False


def _merge_with_existing_xmp(
    vip_fields: dict,
    existing: dict,
    all_vip_names: set[str] | None = None,
) -> dict:
    """
    Merge VIP-generated fields with the metadata already present in the file,
    so that repeated writebacks and edits from other apps are never destroyed.

    Rules applied per field:
    - XMP:PersonInImage  — union(VIP persons, existing file persons).
                           VIP is the authority for its own persons; external
                           persons (added by Lightroom, Photos, etc.) are kept.
                           If *all_vip_names* is provided, previously-written
                           VIP person names (e.g. a merged-away "Bob") are NOT
                           preserved — they are superseded by the current set.
    - XMP:Subject /
      IPTC:Keywords      — VIP-prefixed keywords (obj:, animal:, geo:, place:)
                           are replaced with the current DB values.
                           All other existing keywords (Lightroom tags, custom
                           keywords) are preserved and merged in.
    - All other fields   — VIP value wins (GPS, XMP:Location, XMP:Identifier,
                           XMP-mwg-rs:Regions).
    """
    merged = dict(vip_fields)

    # ── PersonInImage: union — keep truly external persons, drop VIP-old ones ──
    file_persons = existing.get("XMP:PersonInImage", [])
    if isinstance(file_persons, str):
        file_persons = [file_persons]
    vip_persons = list(vip_fields.get("XMP:PersonInImage", []))
    if file_persons:
        # Preserve order: VIP persons first, then file-only persons appended.
        vip_set = set(vip_persons)
        # When all_vip_names is supplied we can distinguish between:
        #   • truly external persons (written by Lightroom, Photos, etc.) → keep
        #   • previously VIP-written persons that are now merged/renamed → drop
        # Without all_vip_names we fall back to the conservative union.
        if all_vip_names:
            extra = [
                p for p in file_persons
                if p not in vip_set and p not in all_vip_names
            ]
        else:
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


async def _load_all_vip_names() -> set[str]:
    """
    Return the set of every person name VIP has ever managed (including
    persons that were subsequently merged away).  Used by _merge_with_existing_xmp
    to distinguish truly-external file persons from VIP ones that need replacing.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT name FROM persons WHERE name IS NOT NULL"
        )
    return {r["name"] for r in rows}


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

    # Load all VIP-ever-known person names so that merged-away names in files
    # get replaced on the next writeback rather than preserved as "external".
    all_vip_names = await _load_all_vip_names()

    # Load any enabled remote server configs for SSH dispatch.
    remote_servers = await _load_remote_servers()

    # Run all ExifTool writes in a single executor thread using the persistent
    # stay_open process — eliminates one Perl interpreter startup per file.
    loop = asyncio.get_event_loop()

    def _do_writes() -> list[tuple[int, int, bool, str]]:
        """Returns list of (queue_id, media_id, success, msg)."""
        # Only pre-read existing XMP from files that have been written before
        # (writeback_done=1). First-write files have no VIP metadata to merge,
        # so skipping their NAS round-trip avoids significant I/O overhead.
        # Note: reads use the local mount path (fast -fast2) even for remote files.
        rewrite_paths = [
            Path(r["file_path"]) for r in rows
            if bool(r["writeback_done"]) and Path(r["file_path"]).exists()
        ]
        existing_xmp = ExifToolWriter.read_xmp_fields(rewrite_paths) if rewrite_paths else {}

        # Separate rows into local (persistent ExifTool) and remote (SSH) buckets.
        local_rows: list[dict] = []
        remote_rows: list[tuple[dict, dict]] = []  # (row, server)
        for row in rows:
            srv = _match_remote_server(row["file_path"], remote_servers)
            if srv:
                remote_rows.append((row, srv))
            else:
                local_rows.append(row)

        # Clean up any stale _exiftool_tmp files left by a previous interrupted
        # run — ExifTool refuses to write when its temp file already exists.
        for row in local_rows:
            tmp = Path(row["file_path"] + "_exiftool_tmp")
            if tmp.exists():
                try:
                    tmp.unlink()
                    logger.info("Removed stale tmp file: %s", tmp)
                except OSError as e:
                    logger.warning("Could not remove stale tmp %s: %s", tmp, e)

        results: list[tuple[int, int, bool, str]] = []

        # ── Local writes — persistent ExifTool stay_open process ──────────
        writer = ExifToolWriter()
        writer.open()
        try:
            for row in local_rows:
                queue_id = row["id"]
                media_id = row["media_file_id"]
                file_path = Path(row["file_path"])
                is_first_write = not bool(row["writeback_done"])
                vip_fields = all_fields.get(media_id, {})

                if not vip_fields:
                    logger.info("No fields to write for media_id=%d, skipping", media_id)
                    results.append((queue_id, media_id, True, "skipped"))
                    continue

                # If the file is an iCloud eviction stub, try to trigger a
                # download before handing it to ExifTool.  This avoids the
                # common "not on disk" failure for Photos that were offloaded
                # to iCloud while still indexed in VIP.
                if not file_path.exists():
                    icloud_stub = file_path.parent / f".{file_path.name}.icloud"
                    if icloud_stub.exists():
                        logger.info(
                            "iCloud stub detected for %s — triggering download "
                            "(timeout 60 s)", file_path.name,
                        )
                        if _try_icloud_download(file_path):
                            logger.info(
                                "iCloud download complete for %s", file_path.name
                            )
                        else:
                            msg = (
                                f"iCloud file did not download within 60 s: "
                                f"{file_path.name}. Open the file in Finder first, "
                                "then retry writeback."
                            )
                            logger.warning(msg)
                            results.append((queue_id, media_id, False, msg))
                            continue
                    elif len(file_path.parts) >= 3 and file_path.parts[1] == "Volumes":
                        # Mounted network volume — file is missing or volume is
                        # not connected.  Emit a targeted warning so the user
                        # knows this is a network-access issue, not a local one.
                        volume_root = Path(
                            file_path.parts[0], file_path.parts[1], file_path.parts[2]
                        )
                        if not volume_root.exists():
                            logger.warning(
                                "Skipping %s — network volume not mounted: %s. "
                                "Re-mount the volume then retry, or configure it as a "
                                "Remote Server in Admin to write via SSH.",
                                file_path.name, volume_root,
                            )
                        else:
                            logger.warning(
                                "Skipping %s — file not found on volume '%s'. "
                                "It may have been moved or deleted on the remote server.",
                                file_path.name, file_path.parts[2],
                            )

                existing = existing_xmp.get(str(file_path), {})
                fields = _merge_with_existing_xmp(vip_fields, existing, all_vip_names)

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

        # ── Remote writes — ExifTool executed on the remote host via SSH ──
        if remote_rows:
            # Group by server so each server uses its own concurrency pool.
            by_server: dict[int, tuple[dict, list[tuple[dict, dict]]]] = {}
            for row, srv in remote_rows:
                sid = srv["id"]
                if sid not in by_server:
                    by_server[sid] = (srv, [])
                by_server[sid][1].append((row, srv))

            for sid, (srv, srv_rows) in by_server.items():
                concurrency = max(1, srv.get("writeback_concurrency", 4))

                def _write_remote_row(item: tuple[dict, dict]) -> tuple[int, int, bool, str]:
                    row, server = item
                    queue_id = row["id"]
                    media_id = row["media_file_id"]
                    is_first_write = not bool(row["writeback_done"])
                    vip_fields = all_fields.get(media_id, {})

                    if not vip_fields:
                        logger.info(
                            "No fields to write for media_id=%d (remote), skipping", media_id
                        )
                        return (queue_id, media_id, True, "skipped")

                    file_path_str = row["file_path"]
                    existing = existing_xmp.get(file_path_str, {})
                    fields = _merge_with_existing_xmp(vip_fields, existing, all_vip_names)
                    remote_path = _translate_path(file_path_str, server)

                    success, msg = ExifToolWriter.write_remote(
                        remote_path,
                        fields,
                        host=server["host"],
                        port=server["port"],
                        user=server["user"],
                        ssh_key_path=server["ssh_key_path"],
                        is_first_write=is_first_write,
                    )
                    logger.info(
                        "Remote write %s for %s → %s: %s",
                        "OK" if success else "FAILED",
                        file_path_str, remote_path, msg,
                    )
                    return (queue_id, media_id, success, msg)

                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = [pool.submit(_write_remote_row, item) for item in srv_rows]
                    for fut in as_completed(futures):
                        results.append(fut.result())

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

    meta_by_mid: dict[int, object] = {r["id"]: r for r in meta_rows}

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

        meta    = meta_by_mid.get(media_id)
        gps_lat = meta["gps_lat"] if meta else None
        gps_lon = meta["gps_lon"] if meta else None
        vip_id  = meta["vip_id"]  if meta else None

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

    file_path = Path(row["file_path"])

    # iCloud stub: either the DB flag is set, or there is a filesystem
    # placeholder next to the expected path.  Attempt to trigger a download
    # before giving up — this recovers the common "file evicted to iCloud"
    # case without requiring manual intervention from the user.
    icloud_stub = file_path.parent / f".{file_path.name}.icloud"
    if row["is_stub"] or (not file_path.exists() and icloud_stub.exists()):
        logger.info(
            "write_single_file: iCloud stub detected for %s — triggering download",
            file_path.name,
        )
        loop = asyncio.get_event_loop()
        available = await loop.run_in_executor(None, _try_icloud_download, file_path)
        if not available:
            raise ValueError(
                f"iCloud file did not download within 60 s: {file_path.name}. "
                "Open the file in Finder first, then retry."
            )

    if not file_path.exists():
        raise ValueError(ExifToolWriter._diagnose_missing_file(file_path))

    fields = await _build_fields_for_file(media_file_id)
    if not fields:
        return {"status": "skipped", "reason": "No metadata to write yet", "fields_written": []}

    # Merge with what's already in the file so existing persons/keywords survive.
    # Skip the NAS read entirely on first write — nothing to preserve yet.
    is_first_write = not bool(row["writeback_done"])
    if not is_first_write:
        loop = asyncio.get_event_loop()
        existing_xmp = await loop.run_in_executor(
            None, ExifToolWriter.read_xmp_fields, [file_path]
        )
        existing = existing_xmp.get(str(file_path), {})
        all_vip_names = await _load_all_vip_names()
        fields = _merge_with_existing_xmp(fields, existing, all_vip_names)

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
