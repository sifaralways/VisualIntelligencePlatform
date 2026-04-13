"""VIP API — Search routes."""

import asyncio
import logging
import os
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import numpy as np

from backend.database.db import get_db
from backend.ml.clip_index import ClipFaissIndex
from backend.ml.query_router import QueryRouter, OllamaUnavailableError
from backend.pipeline import ingest as ingest_pipeline

router = APIRouter()
_query_router = QueryRouter()
_clip_index = ClipFaissIndex()
logger = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    query: str = ""
    person_ids: Optional[list[int]] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    camera_make: Optional[str] = None
    limit: int = 50
    offset: int = 0


class NaturalSearchRequest(BaseModel):
    query: str
    limit: int = 50


@router.post("")
async def search(req: SearchRequest):
    """
    Search media files. All filters are optional and combinable.
    Operates entirely on the local SQLite DB — works even when files
    are offloaded to iCloud.
    """
    conditions = []
    params: list = []

    if req.query:
        # Simple keyword search across path, camera model, date
        like = f"%{req.query}%"
        conditions.append("""
            (mf.file_path LIKE ?
             OR mf.camera_model LIKE ?
             OR mf.date_taken LIKE ?
             OR p.name LIKE ?)
        """)
        params.extend([like, like, like, like])

    if req.person_ids:
        placeholders = ",".join("?" * len(req.person_ids))
        conditions.append(f"p.id IN ({placeholders})")
        params.extend(req.person_ids)

    if req.date_from:
        conditions.append("mf.date_taken >= ?")
        params.append(req.date_from)

    if req.date_to:
        conditions.append("mf.date_taken <= ?")
        params.append(req.date_to)

    if req.camera_make:
        conditions.append("mf.camera_make LIKE ?")
        params.append(f"%{req.camera_make}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([req.limit, req.offset])

    query_sql = f"""
        SELECT DISTINCT mf.id, mf.file_path, mf.date_taken, mf.camera_model,
               mf.width, mf.height, mf.ingest_state,
               GROUP_CONCAT(DISTINCT p.name) as persons
        FROM media_files mf
        LEFT JOIN faces f ON f.media_file_id = mf.id
        LEFT JOIN persons p ON p.id = f.person_id AND p.is_merged = 0
        {where}
        GROUP BY mf.id
        ORDER BY mf.date_taken DESC
        LIMIT ? OFFSET ?
    """

    async with get_db() as db:
        rows = await db.execute_fetchall(query_sql, params)

    return {
        "results": [dict(r) for r in rows],
        "count": len(rows),
        "limit": req.limit,
        "offset": req.offset,
    }


def _ensure_clip_ready() -> None:
    ingest_pipeline._ensure_models()
    if _clip_index.total == 0:
        _clip_index.load()


def _encode_text_clip_sync(text: str, expected_dim: int | None) -> np.ndarray | None:
    import open_clip
    import torch

    landmark = ingest_pipeline._tagger._landmark
    model = landmark._clip_model
    device = landmark._device
    if model is None:
        return None

    candidates = [
        os.environ.get("VIP_LANDMARK_CLIP_MODEL", "ViT-L-14"),
        "ViT-B-32",
    ]

    for model_name in candidates:
        try:
            tokenizer = open_clip.get_tokenizer(model_name)
            tokens = tokenizer([text]).to(device)
            with torch.no_grad():
                text_vec = model.encode_text(tokens)
                text_vec = text_vec / text_vec.norm(dim=-1, keepdim=True)
            vec = text_vec.squeeze(0).detach().cpu().numpy().astype(np.float32)
            if expected_dim and vec.shape[0] != expected_dim:
                continue
            return vec
        except Exception:
            continue

    return None


async def _encode_text_clip(text: str, expected_dim: int | None) -> np.ndarray | None:
    _ensure_clip_ready()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, ingest_pipeline._tagger.load)
    return await loop.run_in_executor(None, _encode_text_clip_sync, text, expected_dim)


async def _execute_router_sql(sql: str, limit: int) -> tuple[list[int], str | None]:
    wrapped = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS planned LIMIT ?"
    try:
        async with get_db() as db:
            rows = await db.execute_fetchall(wrapped, (limit,))
    except Exception as exc:
        logger.warning("Natural search planner SQL failed: %s | sql=%s", exc, sql)
        return [], f"Generated SQL could not be executed: {exc}"

    payloads = [dict(row) for row in rows]
    media_ids = _extract_media_ids_from_rows(payloads)

    if not media_ids:
        media_ids = await _fallback_cooccurrence_media_ids(payloads, limit)

    if not media_ids and payloads:
        return [], "Generated SQL did not return media_id rows"

    # Stable dedupe while preserving planner rank.
    deduped: list[int] = []
    seen: set[int] = set()
    for media_id in media_ids:
        if media_id in seen:
            continue
        seen.add(media_id)
        deduped.append(media_id)
    return deduped, None


def _extract_media_ids_from_rows(payloads: list[dict]) -> list[int]:
    media_ids: list[int] = []
    for payload in payloads:
        if "media_id" in payload:
            media_ids.append(int(payload["media_id"]))
        elif "id" in payload:
            media_ids.append(int(payload["id"]))
    return media_ids


async def _fallback_cooccurrence_media_ids(payloads: list[dict], limit: int) -> list[int]:
    if not payloads:
        return []

    sample = payloads[0]
    if not {"person_a", "person_b", "shared_photo_count"}.issubset(sample.keys()):
        return []

    person_a = str(sample.get("person_a") or "").strip()
    person_b = str(sample.get("person_b") or "").strip()
    if not person_a or not person_b:
        return []

    async with get_db() as db:
        pair_rows = await db.execute_fetchall(
            """
            SELECT a.media_id
            FROM v_person_photos a
            JOIN v_person_photos b ON b.media_id = a.media_id
            WHERE a.person_name LIKE ?
              AND b.person_name LIKE ?
            ORDER BY a.date_taken DESC
            LIMIT ?
            """,
            (f"%{person_a}%", f"%{person_b}%", limit),
        )
    return [int(r["media_id"]) for r in pair_rows]


async def _hydrate_media_rows(
    media_ids: list[int],
    clip_scores: dict[int, float] | None = None,
    sql_matched_ids: set[int] | None = None,
) -> list[dict]:
    if not media_ids:
        return []

    placeholders = ",".join("?" * len(media_ids))
    rank = {media_id: idx for idx, media_id in enumerate(media_ids)}

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""
            SELECT
                mf.id AS media_id,
                mf.file_path,
                mf.date_taken,
                GROUP_CONCAT(DISTINCT p.name) AS persons,
                GROUP_CONCAT(DISTINCT mt.label) AS tags
            FROM media_files mf
            LEFT JOIN faces f ON f.media_file_id = mf.id
            LEFT JOIN persons p ON p.id = f.person_id AND p.is_merged = 0
            LEFT JOIN media_tags mt ON mt.media_file_id = mf.id
            WHERE mf.id IN ({placeholders})
              AND mf.removed_from_app = 0
            GROUP BY mf.id
            """,
            media_ids,
        )

    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item["persons"] = item["persons"].split(",") if item.get("persons") else []
        item["tags"] = item["tags"].split(",") if item.get("tags") else []
        mid = int(item["media_id"])
        if clip_scores and mid in clip_scores:
            item["clip_score"] = round(float(clip_scores[mid]), 4)
        if sql_matched_ids is not None:
            item["sql_matched"] = mid in sql_matched_ids
        out.append(item)

    out.sort(key=lambda x: rank.get(int(x["media_id"]), 10**9))
    return out


@router.post("/natural")
async def natural_search(req: NaturalSearchRequest):
    query = (req.query or "").strip()
    if not query:
        return {
            "intent": "SQL_ONLY",
            "explanation": "Empty query",
            "results": [],
            "count": 0,
        }

    limit = max(1, min(int(req.limit or 50), 200))

    try:
        plan = await _query_router.route(query)
    except OllamaUnavailableError:
        return {
            "intent": "SQL_ONLY",
            "explanation": "Ollama is unavailable. Start Ollama and retry natural search.",
            "results": [],
            "count": 0,
            "error": "ollama_unavailable",
        }

    if plan.intent == "SQL_ONLY" and plan.sql:
        return await _natural_sql_only(plan, limit)

    if plan.intent == "CLIP_ONLY":
        return await _natural_clip_only(plan, query, limit)

    return await _natural_hybrid(plan, query, limit)


async def _natural_sql_only(plan, limit: int) -> dict:
    media_ids, sql_error = await _execute_router_sql(plan.sql, limit)
    if sql_error:
        return {
            "intent": "SQL_ONLY",
            "explanation": f"{plan.explanation} (planner SQL was invalid)",
            "results": [],
            "count": 0,
            "error": "invalid_sql_plan",
        }
    results = await _hydrate_media_rows(media_ids, sql_matched_ids=set(media_ids))
    return {
        "intent": "SQL_ONLY",
        "explanation": plan.explanation,
        "results": results,
        "count": len(results),
    }


async def _natural_clip_only(plan, query: str, limit: int) -> dict:
    _ensure_clip_ready()
    if _clip_index.total == 0:
        return {
            "intent": "CLIP_ONLY",
            "explanation": "CLIP index is empty. Run or re-run ingest to build photo embeddings.",
            "results": [],
            "count": 0,
        }

    clip_text = plan.clip_description or query
    text_vec = await _encode_text_clip(clip_text, _clip_index.dimension)
    if text_vec is None:
        return {
            "intent": "CLIP_ONLY",
            "explanation": "Unable to encode CLIP text query",
            "results": [],
            "count": 0,
        }

    hits = _clip_index.search(text_vec, k=limit, threshold=0.20)
    media_ids = [media_id for media_id, _ in hits]
    clip_scores = dict(hits)
    results = await _hydrate_media_rows(media_ids, clip_scores=clip_scores)
    return {
        "intent": "CLIP_ONLY",
        "explanation": plan.explanation,
        "results": results,
        "count": len(results),
    }


async def _natural_hybrid(plan, query: str, limit: int) -> dict:
    candidate_ids = []
    if plan.sql:
        candidate_ids, sql_error = await _execute_router_sql(plan.sql, max(limit * 4, 120))
        if sql_error:
            return {
                "intent": "HYBRID",
                "explanation": f"{plan.explanation} (planner SQL was invalid)",
                "results": [],
                "count": 0,
                "error": "invalid_sql_plan",
            }

    if not candidate_ids:
        return {
            "intent": "HYBRID",
            "explanation": plan.explanation,
            "results": [],
            "count": 0,
        }

    clip_text = plan.clip_description or query
    text_vec = await _encode_text_clip(clip_text, None)
    if text_vec is None:
        results = await _hydrate_media_rows(candidate_ids[:limit], sql_matched_ids=set(candidate_ids))
        return {
            "intent": "HYBRID",
            "explanation": f"{plan.explanation} (CLIP rerank unavailable, returning SQL matches)",
            "results": results,
            "count": len(results),
        }

    placeholders = ",".join("?" * len(candidate_ids))
    async with get_db() as db:
        vec_rows = await db.execute_fetchall(
            f"""
            SELECT media_file_id, vector
            FROM clip_embeddings
            WHERE media_file_id IN ({placeholders})
            """,
            candidate_ids,
        )

    clip_scores: dict[int, float] = {}
    for row in vec_rows:
        media_id = int(row["media_file_id"])
        vec = np.frombuffer(row["vector"], dtype=np.float32)
        if vec.shape[0] != text_vec.shape[0]:
            continue
        clip_scores[media_id] = float(np.dot(text_vec, vec))

    ranked_ids = sorted(
        candidate_ids,
        key=lambda media_id: clip_scores.get(media_id, -1.0),
        reverse=True,
    )[:limit]

    results = await _hydrate_media_rows(
        ranked_ids,
        clip_scores=clip_scores,
        sql_matched_ids=set(candidate_ids),
    )
    return {
        "intent": "HYBRID",
        "explanation": plan.explanation,
        "results": results,
        "count": len(results),
    }
