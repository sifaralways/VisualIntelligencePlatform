"""VIP API — Search routes."""

import asyncio
import json
import logging
import os
import re
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import numpy as np
import httpx

from backend.config import settings
from backend.database.db import get_db
from backend.ml.clip_index import ClipFaissIndex
from backend.ml.query_router import QueryRouter, OllamaUnavailableError
from backend.pipeline import ingest as ingest_pipeline

router = APIRouter()
_query_router = QueryRouter()
_clip_index = ClipFaissIndex()
logger = logging.getLogger(__name__)


_SQL_REPAIR_SYSTEM_PROMPT = """
You repair SQLite SELECT queries for a photo search engine.
Return JSON only, no markdown fences.

Output schema:
{
    "sql": string,
    "explanation": string
}

Hard constraints:
- Output one read-only SELECT/WITH query only.
- Query must be SQLite compatible.
- Query must return a column named media_id.
- No INSERT/UPDATE/DELETE/ALTER/DROP/PRAGMA/ATTACH.

Preferred views:
- v_photo_full_context(media_id, ...)
- v_person_photos(person_name, media_id, ...)
- v_photo_persons_agg(media_id, person_count, person_names, ...)
- v_photo_tags_flat(media_id, category, label, ...)
- v_photos_with_location(media_id, place_label, ...)
- v_photos_active(media_id, ...)

Use LIKE '%name%' for person matching instead of exact equality.
""".strip()


_CONFIDENCE_MIN_EXECUTE = 0.25
_CONFIDENCE_LOW = 0.45
_QUERY_STOPWORDS = {
    "show",
    "me",
    "photos",
    "photo",
    "images",
    "image",
    "with",
    "from",
    "in",
    "of",
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "for",
    "by",
    "on",
}


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


def _wildcard_token_to_like(token: str) -> str:
    """Convert old-school wildcard token to SQL LIKE pattern.

    Supported wildcards:
    - * => %
    - ? => _
    """
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("*", "%").replace("?", "_")


def _parse_search_tokens(raw: str) -> list[tuple[str, bool]]:
    """Parse query into tokens and mark quoted tokens.

    Returns list of (token, is_quoted).
    """
    tokens: list[tuple[str, bool]] = []
    for match in re.finditer(r'"([^"\\]*(?:\\.[^"\\]*)*)"|(\S+)', raw or ""):
        quoted = match.group(1)
        plain = match.group(2)
        if quoted is not None:
            token = quoted.replace('\\"', '"').strip()
            if token:
                tokens.append((token, True))
        elif plain is not None:
            token = plain.strip()
            if token:
                tokens.append((token, False))
    return tokens


def _quoted_whole_word_like(token: str) -> str:
    # Normalize punctuation to spaces so "Audi" does not match "Auditorium".
    normalized = re.sub(r"[^A-Za-z0-9]+", " ", token).strip().lower()
    if not normalized:
        normalized = token.strip().lower()
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"% {escaped} %"


@router.post("")
async def search(req: SearchRequest):
    """
    Search media files. All filters are optional and combinable.
    Operates entirely on the local SQLite DB — works even when files
    are offloaded to iCloud.
    """
    conditions: list[str] = []
    params: list = []

    if req.query:
        # Classic tokenized wildcard search across path/person/tags/florence text.
        tokens = _parse_search_tokens(req.query.strip())
        for token, is_quoted in tokens:
            if is_quoted:
                like = _quoted_whole_word_like(token)
                conditions.append(
                    """
                    (
                        (' ' || lower(replace(replace(replace(COALESCE(pfc.file_path, ''), '_', ' '), '-', ' '), '.', ' ')) || ' ') LIKE ? ESCAPE '\\'
                        OR (' ' || lower(replace(replace(replace(COALESCE(pfc.persons, ''), '_', ' '), '-', ' '), '.', ' ')) || ' ') LIKE ? ESCAPE '\\'
                        OR (' ' || lower(replace(replace(replace(COALESCE(tags.all_tags, ''), '_', ' '), '-', ' '), '.', ' ')) || ' ') LIKE ? ESCAPE '\\'
                        OR (' ' || lower(replace(replace(replace(COALESCE(text.all_text, ''), '_', ' '), '-', ' '), '.', ' ')) || ' ') LIKE ? ESCAPE '\\'
                    )
                    """
                )
                params.extend([like, like, like, like])
            else:
                has_user_wildcard = ("*" in token) or ("?" in token)
                like = _wildcard_token_to_like(token)
                # If no wildcard is supplied, default to contains-match.
                if not has_user_wildcard:
                    like = f"%{like}%"
                conditions.append(
                    """
                    (
                        lower(pfc.file_path) LIKE lower(?) ESCAPE '\\'
                        OR lower(COALESCE(pfc.persons, '')) LIKE lower(?) ESCAPE '\\'
                        OR lower(COALESCE(tags.all_tags, '')) LIKE lower(?) ESCAPE '\\'
                        OR lower(COALESCE(text.all_text, '')) LIKE lower(?) ESCAPE '\\'
                    )
                    """
                )
                params.extend([like, like, like, like])

    if req.person_ids:
        placeholders = ",".join("?" * len(req.person_ids))
        conditions.append(
            f"""
            EXISTS (
                SELECT 1
                FROM faces f
                JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                JOIN persons p ON p.person_guid = cpc.person_guid
                WHERE f.media_file_id = pfc.media_id
                  AND p.is_merged = 0
                  AND p.id IN ({placeholders})
            )
            """
        )
        params.extend(req.person_ids)

    if req.date_from:
        conditions.append("pfc.date_taken >= ?")
        params.append(req.date_from)

    if req.date_to:
        conditions.append("pfc.date_taken <= ?")
        params.append(req.date_to)

    if req.camera_make:
        conditions.append("pfc.camera_make LIKE ?")
        params.append(f"%{req.camera_make}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([req.limit, req.offset])

    query_sql = f"""
        SELECT pfc.media_id AS id,
               pfc.file_path,
               pfc.date_taken,
               pfc.camera_model,
               pfc.persons,
               tags.all_tags AS tags
        FROM v_photo_full_context pfc
        LEFT JOIN (
            SELECT mt.media_file_id, GROUP_CONCAT(DISTINCT mt.label) AS all_tags
            FROM media_tags mt
            GROUP BY mt.media_file_id
        ) AS tags ON tags.media_file_id = pfc.media_id
        LEFT JOIN v_photo_text_agg text ON text.media_id = pfc.media_id
        {where}
        ORDER BY pfc.date_taken DESC, pfc.media_id DESC
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


def _parse_llm_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


async def _repair_router_sql(original_sql: str, user_query: str, sql_error: str) -> str | None:
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "messages": [
            {"role": "system", "content": _SQL_REPAIR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_query": user_query,
                        "failed_sql": original_sql,
                        "execution_error": sql_error,
                    }
                ),
            },
        ],
        "options": {"temperature": 0.0},
    }
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    except Exception as exc:
        logger.warning("SQL repair unavailable: %s", exc)
        return None

    content = ((response.json().get("message") or {}).get("content") or "").strip()
    parsed = _parse_llm_json(content)
    repaired_sql = _query_router._sanitize_sql(parsed.get("sql"))
    if not repaired_sql:
        return None
    if repaired_sql.strip() == (original_sql or "").strip():
        return None
    return repaired_sql


async def _execute_router_sql_with_retry(sql: str, limit: int, user_query: str) -> tuple[list[int], str | None, bool]:
    media_ids, sql_error = await _execute_router_sql(sql, limit)
    if not sql_error:
        return media_ids, None, False

    repaired_sql = await _repair_router_sql(sql, user_query, sql_error)
    if not repaired_sql:
        return [], sql_error, False

    media_ids, repair_error = await _execute_router_sql(repaired_sql, limit)
    if repair_error:
        return [], repair_error, False
    return media_ids, None, True


def _estimate_sql_plan_confidence(query: str, sql: str | None, intent: str) -> tuple[float, list[str]]:
    if intent == "CLIP_ONLY":
        return 1.0, ["clip_only"]
    if not sql:
        return 0.0, ["missing_sql"]

    score = 1.0
    reasons: list[str] = []
    sql_norm = " ".join(sql.lower().split())

    if " media_id" not in f" {sql_norm} ":
        score -= 0.5
        reasons.append("missing_media_id_projection")
    if " where " not in f" {sql_norm} ":
        score -= 0.2
        reasons.append("no_where_clause")
    if re.search(r"\bperson_name\s*=\s*'", sql_norm):
        score -= 0.25
        reasons.append("exact_person_name_match")
    if re.search(r"\blimit\s+\d+", sql_norm) is None:
        score -= 0.1
        reasons.append("no_limit_clause")
    if any(token in sql_norm for token in ("pragma", "attach", "drop", "alter", "delete", "update", "insert")):
        score = 0.0
        reasons.append("forbidden_token_present")

    if any(name in query.lower() for name in ("akshat", "aditi", "maryam", "gordon")) and " like " not in f" {sql_norm} ":
        score -= 0.15
        reasons.append("person_query_without_like")

    return max(0.0, min(score, 1.0)), reasons


def _tokenize_query_for_safe_search(query: str) -> list[str]:
    parts = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", (query or ""))
    tokens: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = part.strip("'\"").lower()
        if len(token) < 3 or token in _QUERY_STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens[:8]


async def _safe_broad_media_ids(query: str, limit: int) -> list[int]:
    tokens = _tokenize_query_for_safe_search(query)
    if not tokens:
        return []

    where_parts: list[str] = []
    params: list[object] = []
    for token in tokens:
        where_parts.append(
            "(" 
            "COALESCE(file_path, '') LIKE ? OR "
            "COALESCE(persons, '') LIKE ? OR "
            "COALESCE(objects, '') LIKE ? OR "
            "COALESCE(places, '') LIKE ? OR "
            "COALESCE(animals, '') LIKE ? OR "
            "COALESCE(scenes, '') LIKE ?"
            ")"
        )
        like = f"%{token}%"
        params.extend([like, like, like, like, like, like])

    sql = (
        "SELECT media_id FROM v_photo_full_context "
        f"WHERE {' AND '.join(where_parts)} "
        "ORDER BY date_taken DESC "
        "LIMIT ?"
    )
    params.append(max(1, min(limit, 200)))

    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params))
    return [int(r["media_id"]) for r in rows if r.get("media_id") is not None]


async def _safe_fallback_results(query: str, limit: int) -> list[dict]:
    fallback_ids = await _safe_broad_media_ids(query, limit)
    return await _hydrate_media_rows(fallback_ids, sql_matched_ids=set(fallback_ids))


def _confidence_meta(confidence: float, reasons: list[str], fallback_used: bool) -> dict:
    return {
        "confidence": confidence,
        "confidence_reasons": reasons,
        "fallback_used": fallback_used,
    }


def _maybe_mark_repaired_explanation(explanation: str, repaired: bool) -> str:
    return f"{explanation} (auto-repaired SQL)" if repaired else explanation


async def _hybrid_safe_fallback_response(plan_explanation: str, query: str, limit: int, confidence: float, reasons: list[str]) -> dict:
    fallback_results = await _safe_fallback_results(query, limit)
    out = {
        "intent": "HYBRID",
        "explanation": f"{plan_explanation} (low SQL confidence; safe fallback used)",
        "results": fallback_results,
        "count": len(fallback_results),
    }
    out.update(_confidence_meta(confidence, reasons, True))
    return out


async def _hybrid_invalid_sql_response(plan_explanation: str, query: str, limit: int, confidence: float, reasons: list[str]) -> dict:
    fallback_results = await _safe_fallback_results(query, limit)
    out = {
        "intent": "HYBRID",
        "explanation": f"{plan_explanation} (planner SQL was invalid)",
        "results": fallback_results,
        "count": len(fallback_results),
        "error": "invalid_sql_plan",
    }
    if fallback_results:
        out["explanation"] = f"{plan_explanation} (planner SQL was invalid; safe fallback used)"
    out.update(_confidence_meta(confidence, reasons, bool(fallback_results)))
    return out


async def _hybrid_no_candidates_response(plan_explanation: str, query: str, limit: int, confidence: float, reasons: list[str]) -> dict:
    out = {
        "intent": "HYBRID",
        "explanation": plan_explanation,
        "results": [],
        "count": 0,
    }
    if confidence < _CONFIDENCE_LOW:
        fallback_results = await _safe_fallback_results(query, limit)
        if fallback_results:
            out["results"] = fallback_results
            out["count"] = len(fallback_results)
            out["explanation"] = f"{plan_explanation} (low-confidence SQL produced no rows; safe fallback used)"
            out.update(_confidence_meta(confidence, reasons, True))
            return out
    out.update(_confidence_meta(confidence, reasons, False))
    return out


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
                        LEFT JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                        LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                        LEFT JOIN persons p ON p.person_guid = cpc.person_guid AND p.is_merged = 0
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

    confidence, reasons = _estimate_sql_plan_confidence(query, plan.sql, plan.intent)

    if plan.intent == "SQL_ONLY" and plan.sql:
        return await _natural_sql_only(plan, query, limit, confidence, reasons)

    if plan.intent == "CLIP_ONLY":
        return await _natural_clip_only(plan, query, limit)

    return await _natural_hybrid(plan, query, limit, confidence, reasons)


async def _natural_sql_only(plan, query: str, limit: int, confidence: float, reasons: list[str]) -> dict:
    if confidence < _CONFIDENCE_MIN_EXECUTE:
        fallback_results = await _safe_fallback_results(query, limit)
        out = {
            "intent": "SQL_ONLY",
            "explanation": f"{plan.explanation} (low SQL confidence; safe fallback used)",
            "results": fallback_results,
            "count": len(fallback_results),
        }
        out.update(_confidence_meta(confidence, reasons, True))
        return out

    media_ids, sql_error, repaired = await _execute_router_sql_with_retry(plan.sql, limit, query)
    if sql_error:
        fallback_results = await _safe_fallback_results(query, limit)
        out = {
            "intent": "SQL_ONLY",
            "explanation": f"{plan.explanation} (planner SQL was invalid)",
            "results": fallback_results,
            "count": len(fallback_results),
            "error": "invalid_sql_plan",
        }
        if fallback_results:
            out["explanation"] = f"{plan.explanation} (planner SQL was invalid; safe fallback used)"
        out.update(_confidence_meta(confidence, reasons, bool(fallback_results)))
        return out

    results = await _hydrate_media_rows(media_ids, sql_matched_ids=set(media_ids))
    if not results and confidence < _CONFIDENCE_LOW:
        fallback_results = await _safe_fallback_results(query, limit)
        if fallback_results:
            out = {
                "intent": "SQL_ONLY",
                "explanation": f"{plan.explanation} (low-confidence SQL produced no rows; safe fallback used)",
                "results": fallback_results,
                "count": len(fallback_results),
            }
            out.update(_confidence_meta(confidence, reasons, True))
            return out

    out = {
        "intent": "SQL_ONLY",
        "explanation": _maybe_mark_repaired_explanation(plan.explanation, repaired),
        "results": results,
        "count": len(results),
    }
    out.update(_confidence_meta(confidence, reasons, False))
    return out


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


async def _natural_hybrid(plan, query: str, limit: int, confidence: float, reasons: list[str]) -> dict:
    candidate_ids = []
    if confidence < _CONFIDENCE_MIN_EXECUTE:
        return await _hybrid_safe_fallback_response(plan.explanation, query, limit, confidence, reasons)

    if plan.sql:
        candidate_ids, sql_error, repaired = await _execute_router_sql_with_retry(plan.sql, max(limit * 4, 120), query)
        if sql_error:
            return await _hybrid_invalid_sql_response(plan.explanation, query, limit, confidence, reasons)
        if repaired:
            plan.explanation = f"{plan.explanation} (auto-repaired SQL)"

    if not candidate_ids:
        return await _hybrid_no_candidates_response(plan.explanation, query, limit, confidence, reasons)

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
    out = {
        "intent": "HYBRID",
        "explanation": plan.explanation,
        "results": results,
        "count": len(results),
    }
    out.update(_confidence_meta(confidence, reasons, False))
    return out
