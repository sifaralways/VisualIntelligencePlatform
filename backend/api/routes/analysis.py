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
    """
    async with get_db() as db:
        # 1. Load model document
        pa_row = await (await db.execute(
            "SELECT model_document, model_version, generated_at, updated_at "
            "FROM photo_analysis WHERE media_file_id = ?",
            (media_id,)
        )).fetchone()

        if pa_row is None:
            raise HTTPException(status_code=404, detail="No analysis document yet. Run the pipeline.")

        try:
            doc: dict = json.loads(pa_row["model_document"])
        except Exception:
            raise HTTPException(status_code=500, detail="Corrupt analysis document")

        doc["model_version"] = pa_row["model_version"]
        doc["updated_at"]    = pa_row["updated_at"]

        # 2. Resolve person_id → person_name in Faces[]
        person_ids = [f["person_id"] for f in doc.get("Faces", []) if f.get("person_id")]
        person_name_map: dict[int, str] = {}
        if person_ids:
            ph = ",".join("?" * len(person_ids))
            rows = await db.execute_fetchall(
                f"SELECT id, name FROM persons WHERE id IN ({ph}) AND is_merged=0",
                person_ids,
            )
            person_name_map = {r["id"]: r["name"] for r in rows if r["name"]}

        for face in doc.get("Faces", []):
            pid = face.get("person_id")
            face["person_name"] = person_name_map.get(pid) if pid else None

        # 3. Load amendments
        amendments = await db.execute_fetchall(
            "SELECT label_name, action, user_value, user_confidence "
            "FROM photo_analysis_amendments WHERE media_file_id = ?",
            (media_id,)
        )
        amend_map = {r["label_name"]: r for r in amendments}

        # 4. Apply amendments to Labels[]
        labels_out: list[dict] = []
        for label in doc.get("Labels", []):
            name = label["Name"]
            amend = amend_map.get(name)
            if amend is None:
                label["UserEdited"] = False
                label["UserConfirmed"] = False
                labels_out.append(label)
            elif amend["action"] == "delete":
                pass  # omit from output
            elif amend["action"] == "rename":
                label = dict(label)
                label["Name"]          = amend["user_value"]
                label["OriginalName"]  = name
                label["UserEdited"]    = True
                label["UserConfirmed"] = False
                if amend["user_confidence"] is not None:
                    label["Confidence"] = amend["user_confidence"]
                labels_out.append(label)
            elif amend["action"] == "confirm":
                label = dict(label)
                label["UserEdited"]    = False
                label["UserConfirmed"] = True
                labels_out.append(label)
            else:
                label["UserEdited"] = False
                label["UserConfirmed"] = False
                labels_out.append(label)

        # Append user-added labels
        for lname, amend in amend_map.items():
            if amend["action"] == "add":
                labels_out.append({
                    "Name":          lname,
                    "Confidence":    amend["user_confidence"] or 100.0,
                    "Source":        "user",
                    "Instances":     [],
                    "Parents":       [],
                    "Categories":    [{"Name": "User Defined"}],
                    "Aliases":       [],
                    "UserEdited":    True,
                    "UserConfirmed": False,
                })

        doc["Labels"] = labels_out

    return doc


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
