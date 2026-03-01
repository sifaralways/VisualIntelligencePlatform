"""
VIP API — Photo analysis routes.

Endpoints:
  GET  /api/analysis/{media_id}          — merged effective document (model + user amendments)
  GET  /api/analysis/{media_id}/raw      — raw model document only (no amendments applied)
  POST /api/analysis/{media_id}/rebuild  — trigger Phase 5 for a single photo (background)
  GET  /api/analysis/{media_id}/amendments — list all amendments for a photo
  PUT  /api/analysis/{media_id}/amend    — add/update a user amendment
  DELETE /api/analysis/{media_id}/amend/{label_name} — remove an amendment (restores model label)

Merging logic (GET /api/analysis/{media_id}):
  1. Start with model_document (JSON blob from photo_analysis table)
  2. Resolve person_id → person_name from persons table (so names are always current)
  3. Apply amendments from photo_analysis_amendments:
       rename  → change label Name to user_value
       delete  → remove label from Labels[]
       add     → append new label to Labels[]
       confirm → mark label UserConfirmed=True (no name change)
  4. Return merged document
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from backend.database.db import get_db
from backend.ml.analysis_builder import merge_analysis_document

logger = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class AmendRequest(BaseModel):
    label_name: str
    action: Literal["rename", "delete", "add", "confirm"]
    user_value: str | None = None        # required for 'rename'; ignored for others
    user_confidence: float | None = None # optional override


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/analysis/{media_id}  — effective (merged) document
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{media_id}")
async def get_analysis(media_id: int):
    """
    Return the merged analysis document for a photo.
    person_id references in Faces[] are resolved to current person_name.
    User amendments are applied on top of the model document.
    Includes vip_history, external_history, and vip_pending sections.
    """
    async with get_db() as db:
        # Confirm the media file exists so we can surface a clear 404
        file_row = await (await db.execute(
            "SELECT external_exif FROM media_files WHERE id=?", (media_id,)
        )).fetchone()
        if file_row is None:
            raise HTTPException(status_code=404, detail="Media file not found")

        doc = await merge_analysis_document(media_id, db)

        # Check for pending writeback
        wq_row = await (await db.execute(
            "SELECT status FROM writeback_queue WHERE media_file_id=? LIMIT 1",
            (media_id,)
        )).fetchone()

    if not doc:
        raise HTTPException(status_code=404, detail="No analysis document yet. Run the pipeline.")

    # ── History sections ────────────────────────────────────────────────────
    # external_exif is a one-time snapshot of whatever XMP/IPTC data existed
    # in the file when VIP first imported it (see ingest.py Phase 1).
    #
    #  "VIP History"      → snapshot has "identifier" key (our vip_id UUID)
    #                        meaning VIP previously wrote to this file.
    #  "External History" → snapshot has data but no "identifier"
    #                        meaning another app (Lightroom, Apple Photos, …)
    #                        had already tagged the file.
    #  "VIP Pending"      → writeback_queue has a 'pending' row for this file
    #                        meaning VIP analysis not yet written to disk.
    ext_raw = file_row["external_exif"]
    ext: dict = json.loads(ext_raw) if ext_raw else {}

    if ext.get("identifier"):
        # File was previously processed by VIP — all snapshot data is VIP History
        doc["vip_history"]      = _clean_history(ext)
        doc["external_history"] = None
    elif ext:
        # Pre-existing tags from an external application
        doc["vip_history"]      = None
        doc["external_history"] = _clean_history(ext)
    else:
        doc["vip_history"]      = None
        doc["external_history"] = None

    doc["vip_pending"] = wq_row is not None and wq_row["status"] == "pending"

    return doc


def _clean_history(ext: dict) -> dict:
    """
    Normalise the external_exif snapshot for API output.
    Renames internal keys to friendly names and strips nulls.
    """
    out: dict = {}
    if ext.get("identifier"):
        out["identifier"] = ext["identifier"]
    if ext.get("persons"):
        out["persons"] = ext["persons"]
    if ext.get("keywords"):
        # Split keywords into VIP-namespaced (obj:/geo:/etc.) and plain
        kws: list[str] = ext["keywords"]
        vip_kws   = [k for k in kws if ":" in k]
        plain_kws = [k for k in kws if ":" not in k]
        if vip_kws:
            out["vip_keywords"] = vip_kws
        if plain_kws:
            out["plain_keywords"] = plain_kws
    if ext.get("location"):
        out["location"] = ext["location"]
    if ext.get("region_info"):
        # Extract simple name list from MWG region structs
        region_info = ext["region_info"]
        region_list = region_info.get("RegionList", []) if isinstance(region_info, dict) else []
        named_regions = [
            {
                "name": r.get("Name"),
                "type": r.get("Type"),
                "area": r.get("Area"),
            }
            for r in region_list
            if r.get("Name")
        ]
        if named_regions:
            out["face_regions"] = named_regions
    return out


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/analysis/{media_id}/raw  — unmodified model document
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{media_id}/raw")
async def get_analysis_raw(media_id: int):
    async with get_db() as db:
        row = await (await db.execute(
            "SELECT model_document FROM photo_analysis WHERE media_file_id = ?",
            (media_id,)
        )).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No analysis document yet.")
    return json.loads(row["model_document"])


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/analysis/{media_id}/rebuild  — rebuild doc for one photo
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{media_id}/rebuild")
async def rebuild_analysis(media_id: int, background_tasks: BackgroundTasks):
    async with get_db() as db:
        row = await (await db.execute(
            "SELECT id FROM media_files WHERE id=?", (media_id,)
        )).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    background_tasks.add_task(_rebuild_one, media_id)
    return {"status": "rebuilding", "media_id": media_id}


async def _rebuild_one(media_id: int) -> None:
    from backend.ml.analysis_builder import build_analysis_document, save_analysis_document
    try:
        async with get_db() as db:
            doc = await build_analysis_document(media_id, db)
            await save_analysis_document(media_id, doc, db)
        logger.info("Rebuilt analysis doc for media_id=%d", media_id)
    except Exception as e:
        logger.error("Rebuild failed for media_id=%d: %s", media_id, e)


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/analysis/{media_id}/amendments  — list all user amendments
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{media_id}/amendments")
async def list_amendments(media_id: int):
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT label_name, action, user_value, user_confidence, amended_at "
            "FROM photo_analysis_amendments WHERE media_file_id = ? ORDER BY amended_at",
            (media_id,)
        )
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# PUT /api/analysis/{media_id}/amend  — create or update an amendment
# ─────────────────────────────────────────────────────────────────────────────

@router.put("/{media_id}/amend")
async def upsert_amendment(media_id: int, req: AmendRequest):
    if req.action == "rename" and not req.user_value:
        raise HTTPException(status_code=422, detail="user_value is required for action='rename'")

    async with get_db() as db:
        await db.execute("""
            INSERT INTO photo_analysis_amendments
                (media_file_id, label_name, action, user_value, user_confidence, amended_at)
            VALUES (?,?,?,?,?,datetime('now'))
            ON CONFLICT(media_file_id, label_name) DO UPDATE SET
                action          = excluded.action,
                user_value      = excluded.user_value,
                user_confidence = excluded.user_confidence,
                amended_at      = datetime('now')
        """, (media_id, req.label_name, req.action, req.user_value, req.user_confidence))

    return {"status": "ok", "media_id": media_id, "label_name": req.label_name, "action": req.action}


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/analysis/{media_id}/amend/{label_name}  — undo an amendment
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/{media_id}/amend/{label_name}")
async def delete_amendment(media_id: int, label_name: str):
    async with get_db() as db:
        await db.execute(
            "DELETE FROM photo_analysis_amendments WHERE media_file_id=? AND label_name=?",
            (media_id, label_name)
        )
    return {"status": "deleted", "media_id": media_id, "label_name": label_name}
