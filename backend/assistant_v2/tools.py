from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from backend.api.routes.search import NaturalSearchRequest, natural_search
from backend.api.routes.search import _hydrate_media_rows
from backend.assistant.executor import execute_plan
from backend.assistant.planner import AssistantPlanner
from backend.assistant.types import AssistantState
from backend.config import settings
from backend.database.db import get_db


ToolHandler = Callable[["ToolContext", dict[str, Any]], Awaitable["ToolExecutionResult"]]
_NAME_WORD = r"[A-Za-z]+"
_POSSESSIVE_NAME = rf"({_NAME_WORD}(?:\s+{_NAME_WORD}){{0,10}})"
_SQL_AND = " AND "
_SQL_AGENT_ALLOWED_VIEWS = {
    "v_photos_active",
    "v_person_photos",
    "v_photo_tags_flat",
    "v_photo_text_flat",
    "v_photo_text_agg",
    "v_photo_persons_agg",
    "v_person_cooccurrence_named",
    "v_photos_with_location",
    "v_person_photo_count",
    "v_photos_by_year_month",
    "v_photo_full_context",
}
_SQL_AGENT_FORBIDDEN = (
        "insert ",
        "update ",
        "delete ",
        "drop ",
        "alter ",
        "pragma ",
        "attach ",
        "detach ",
        "create ",
        "replace ",
        "vacuum ",
)
_SQL_AGENT_PROMPT = """
You generate SQLite SELECT queries for VIP analytics chat.
Return JSON only.

Output schema:
{
    "sql": string,
    "reason": string
}

Hard rules:
- One single read-only query only (SELECT or WITH ... SELECT).
- SQLite syntax only.
- Use only these views:
    v_photos_active,
    v_person_photos,
    v_photo_tags_flat,
    v_photo_text_flat,
    v_photo_text_agg,
    v_photo_persons_agg,
    v_person_cooccurrence_named,
    v_photos_with_location,
    v_person_photo_count,
    v_photos_by_year_month,
    v_photo_full_context.
- Never use base tables.
- Do not use PRAGMA/ATTACH/DDL/DML.
- Prefer LIKE for name/place matching (case-insensitive).
- Include media_id when asking for photos.
- Include ORDER BY for deterministic output.
- Do not include trailing semicolons.
- For Florence free-text search (caption/OCR/region), prefer v_photo_text_flat.
- Filter Florence text by text_type when the user intent is specific (ocr/caption/region).
- Join text views to person/location views using media_id for combined constraints.

View columns:
- v_photos_active(media_id, file_path, date_taken, camera_make, camera_model, gps_lat, gps_lon, width, height, file_format, vip_id)
- v_person_photos(person_id, person_name, media_id, file_path, date_taken, camera_make, camera_model, gps_lat, gps_lon)
- v_photo_tags_flat(media_id, file_path, date_taken, gps_lat, gps_lon, category, label, confidence, model)
- v_photo_text_flat(media_id, file_path, date_taken, text_type, text_value, confidence, model)
- v_photo_text_agg(media_id, file_path, date_taken, captions, ocr_text, region_text, all_text)
- v_photo_persons_agg(media_id, file_path, date_taken, camera_make, camera_model, gps_lat, gps_lon, person_count, person_names)
- v_person_cooccurrence_named(person_a, person_b, shared_photo_count, last_seen_at)
- v_photos_with_location(media_id, file_path, date_taken, gps_lat, gps_lon, place_label, place_category)
- v_person_photo_count(person_id, name, photo_count)
- v_photos_by_year_month(media_id, file_path, date_taken, year, month, camera_make, camera_model, gps_lat, gps_lon)
- v_photo_full_context(media_id, file_path, date_taken, camera_make, camera_model, persons, objects, places, animals, scenes)

Examples:
- Photos where OCR mentions invoice: SELECT DISTINCT media_id, file_path FROM v_photo_text_flat WHERE text_type = 'ocr' AND LOWER(text_value) LIKE LOWER('%invoice%') ORDER BY date_taken DESC
- Photos with person + caption phrase: SELECT DISTINCT t.media_id FROM v_photo_text_flat t JOIN v_person_photos p ON p.media_id = t.media_id WHERE LOWER(p.person_name) LIKE LOWER('%alice%') AND t.text_type = 'caption' AND LOWER(t.text_value) LIKE LOWER('%red blanket%') ORDER BY t.media_id DESC
""".strip()
_SQL_AGENT_REPAIR_PROMPT = """
You repair SQLite SELECT queries for VIP analytics.
Return JSON only with schema: {"sql": string, "reason": string}
Rules:
- Output one read-only SELECT/WITH query only.
- Keep to allowed views and valid columns.
- No DML/DDL/PRAGMA/ATTACH.
""".strip()


@dataclass
class ToolSpec:
    name: str
    description: str
    read_only: bool
    input_schema: dict[str, Any]
    handler: ToolHandler


@dataclass
class ToolContext:
    session_id: str
    state: AssistantState
    limit: int
    offset: int


@dataclass
class ToolExecutionResult:
    payload: dict[str, Any]
    next_state: AssistantState
    notes: str | None = None


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    async def execute(self, name: str, ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
        spec = self.get(name)
        if spec is None:
            return ToolExecutionResult(
                payload={
                    "reply_text": f"Unknown tool: {name}",
                    "results": [],
                    "count": 0,
                    "action": "none",
                    "action_payload": {},
                    "intent": "TOOL_ERROR",
                    "error": "unknown_tool",
                },
                next_state=ctx.state,
                notes="tool_not_found",
            )
        return await spec.handler(ctx, params)


def _parse_llm_json(content: str) -> dict[str, Any]:
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


def _extract_sql_identifiers(sql: str) -> set[str]:
    lowered = sql.lower()
    names = set(re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)\b", lowered))
    return names


def _is_safe_sql_agent_query(sql: str) -> bool:
    text = (sql or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if ";" in lowered:
        return False
    if not lowered.startswith(("select", "with")):
        return False
    if any(token in lowered for token in _SQL_AGENT_FORBIDDEN):
        return False

    identifiers = _extract_sql_identifiers(lowered)
    if identifiers and not identifiers.issubset(_SQL_AGENT_ALLOWED_VIEWS):
        return False
    return True


async def _plan_sql_agent_query(message: str, state: AssistantState) -> tuple[str | None, str]:
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "messages": [
            {"role": "system", "content": _SQL_AGENT_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "message": message,
                        "conversation_state": {
                            "last_people": state.last_people,
                            "last_location_term": state.last_location_term,
                            "last_media_ids_count": len(state.last_media_ids),
                        },
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
        return None, f"sql_agent_unavailable:{exc}"

    content = ((response.json().get("message") or {}).get("content") or "").strip()
    parsed = _parse_llm_json(content)
    sql = str(parsed.get("sql") or "").strip()
    reason = str(parsed.get("reason") or "planned by sql agent").strip()
    if not _is_safe_sql_agent_query(sql):
        return None, "unsafe_or_invalid_sql"
    return sql, reason


async def _repair_sql_agent_query(message: str, failed_sql: str, error: str) -> tuple[str | None, str]:
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "messages": [
            {"role": "system", "content": _SQL_AGENT_REPAIR_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "message": message,
                        "failed_sql": failed_sql,
                        "error": error,
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
        return None, f"sql_agent_repair_unavailable:{exc}"

    content = ((response.json().get("message") or {}).get("content") or "").strip()
    parsed = _parse_llm_json(content)
    sql = str(parsed.get("sql") or "").strip()
    reason = str(parsed.get("reason") or "repair attempt").strip()
    if not _is_safe_sql_agent_query(sql):
        return None, "unsafe_or_invalid_repaired_sql"
    return sql, reason


async def _execute_sql_agent_query(sql: str, limit: int, offset: int) -> tuple[list[dict[str, Any]] | None, str | None]:
    wrapped = f"SELECT * FROM ({sql}) AS planned LIMIT ? OFFSET ?"
    try:
        async with get_db() as db:
            rows = await db.execute_fetchall(wrapped, (max(1, min(limit, 200)), offset))
    except Exception as exc:
        return None, str(exc)
    return [dict(r) for r in rows], None


def _format_sql_agent_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No rows returned."
    cols = list(rows[0].keys())[:6]
    lines = [" | ".join(cols)]
    for row in rows[:12]:
        values = [str(row.get(c, ""))[:80] for c in cols]
        lines.append(" | ".join(values))
    if len(rows) > 12:
        lines.append(f"... (+{len(rows) - 12} more rows)")
    return "\n".join(lines)


async def _infer_primary_person_from_message(message: str, state: AssistantState) -> str | None:
    candidate = _extract_subject_person_from_with_location_query(message, state)
    if not candidate:
        m = re.search(r"\bwhere\s+is\s+([\w]+(?:\s+[\w]+){0,10})\s+(?:typically\s+found|usually\s+found|found|seen|been)", message or "", flags=re.IGNORECASE)
        if m:
            candidate = _clean_person_token(m.group(1))
    if not candidate:
        return None
    resolved = await _resolve_person_name(candidate)
    return resolved or candidate


def _sql_agent_no_query_result(ctx: ToolContext) -> ToolExecutionResult:
    return ToolExecutionResult(
        payload={
            "reply_text": "Ask a database question and I can analyze it.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
            "intent": "SQL_ONLY",
            "explanation": "Empty SQL-agent query",
        },
        next_state=ctx.state,
        notes="sql_agent_empty_query",
    )


def _sql_agent_plan_failed_result(ctx: ToolContext, reason: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        payload={
            "reply_text": "I couldn't safely plan a database query for that yet. Try rephrasing with specific entities or filters.",
            "results": [],
            "count": 0,
            "action": "needs_clarification",
            "action_payload": {
                "type": "sql_query_rephrase",
                "example": "Show top 10 locations where Simon appears",
            },
            "intent": "SQL_ONLY",
            "explanation": "SQL agent planning failed",
        },
        next_state=ctx.state,
        notes=f"sql_agent_plan_failed:{reason}",
    )


async def _sql_agent_result_from_rows(
    ctx: ToolContext,
    message: str,
    payload_rows: list[dict[str, Any]],
    reason: str,
) -> ToolExecutionResult:
    media_ids = [int(row["media_id"]) for row in payload_rows if row.get("media_id") is not None]

    if media_ids:
        return await _sql_agent_media_result(ctx, message, media_ids, reason)

    summary = _format_sql_agent_rows(payload_rows)
    payload = {
        "reply_text": (
            f"SQL-agent analysis ({len(payload_rows)} row{'s' if len(payload_rows) != 1 else ''}):\n"
            f"{summary}"
        ),
        "results": [],
        "count": len(payload_rows),
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Open-ended SQL agent answer",
    }
    next_state = ctx.state.model_copy(deep=True)
    inferred_person = await _infer_primary_person_from_message(message, ctx.state)
    if inferred_person:
        next_state.last_people = [inferred_person]
    return ToolExecutionResult(payload=payload, next_state=next_state, notes=f"sql_agent:{reason}")


async def _sql_agent_media_result(
    ctx: ToolContext,
    message: str,
    media_ids: list[int],
    reason: str,
) -> ToolExecutionResult:
    deduped: list[int] = []
    seen: set[int] = set()
    for media_id in media_ids:
        if media_id in seen:
            continue
        seen.add(media_id)
        deduped.append(media_id)

    results = await _hydrate_media_rows(deduped, sql_matched_ids=set(deduped))
    next_state = ctx.state.model_copy(deep=True)
    next_state.last_media_ids = deduped
    inferred_person = await _infer_primary_person_from_message(message, ctx.state)
    if inferred_person:
        next_state.last_people = [inferred_person]

    payload = {
        "reply_text": f"I found {len(deduped)} matching photo{'s' if len(deduped) != 1 else ''} from SQL-agent analysis.",
        "results": results,
        "count": len(deduped),
        "action": "open_search",
        "action_payload": {"query": message},
        "intent": "SQL_ONLY",
        "explanation": "Open-ended SQL agent answer with photo rows",
    }
    return ToolExecutionResult(payload=payload, next_state=next_state, notes=f"sql_agent:{reason}")


async def tool_count_indexed_photos(ctx: ToolContext, _params: dict[str, Any]) -> ToolExecutionResult:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT COUNT(*) AS c
            FROM media_files
            WHERE removed_from_app = 0
              AND is_stub = 0
            """
        )
    total = int(rows[0]["c"]) if rows else 0
    payload = {
        "reply_text": f"So far, {total} photos are indexed in VIP.",
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic count via AssistantV2 tool",
    }
    return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="deterministic_count")


async def tool_count_named_people(ctx: ToolContext, _params: dict[str, Any]) -> ToolExecutionResult:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT COUNT(*) AS c
            FROM persons
            WHERE name IS NOT NULL
              AND name != ''
              AND is_merged = 0
              AND is_ignored = 0
            """
        )
    total = int(rows[0]["c"]) if rows else 0
    payload = {
        "reply_text": f"So far, {total} unique people have been named.",
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic named-people count via AssistantV2 tool",
    }
    return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="deterministic_named_people_count")


async def tool_count_named_faces(ctx: ToolContext, _params: dict[str, Any]) -> ToolExecutionResult:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT COUNT(*) AS c
            FROM faces f
                        JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                        JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                        JOIN persons p ON p.person_guid = cpc.person_guid
            WHERE p.name IS NOT NULL
              AND p.name != ''
              AND p.is_merged = 0
              AND p.is_ignored = 0
            """
        )
    total = int(rows[0]["c"]) if rows else 0
    payload = {
        "reply_text": f"So far, {total} identified faces are mapped to named people.",
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic named-face count via AssistantV2 tool",
    }
    return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="deterministic_named_faces_count")


def _build_unnamed_faces_scope(ctx: ToolContext, params: dict[str, Any]) -> tuple[list[int] | None, dict | None]:
    use_last_results = bool(params.get("use_last_results"))
    media_scope = ctx.state.last_media_ids[:1000] if use_last_results and ctx.state.last_media_ids else None
    if use_last_results and not media_scope:
        payload = {
            "reply_text": "I don't have previous photo results yet. Ask for photos first, then I can show unnamed faces in those photos.",
            "results": [],
            "face_results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
            "intent": "SQL_ONLY",
            "explanation": "Needs prior result scope",
        }
        return None, payload
    return media_scope, None


async def _query_unnamed_faces(media_scope: list[int] | None, limit: int, offset: int) -> tuple[list[dict], int]:
    where = [
        "owner.id IS NULL",
        "mf.removed_from_app = 0",
        "mf.is_stub = 0",
    ]
    params_sql: list[object] = []
    if media_scope:
        placeholders = ",".join("?" for _ in media_scope)
        where.append(f"f.media_file_id IN ({placeholders})")
        params_sql.extend(media_scope)

    sql = (
        "SELECT f.id AS face_id, f.media_file_id AS media_id, f.cluster_id, f.thumbnail_path, "
        "mf.file_path, mf.date_taken, f.detection_conf "
        "FROM faces f "
        "JOIN media_files mf ON mf.id = f.media_file_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(f.detection_conf, 0) DESC, f.id DESC "
        "LIMIT ? OFFSET ?"
    )
    total_sql = (
        "SELECT COUNT(*) AS c "
        "FROM faces f "
        "JOIN media_files mf ON mf.id = f.media_file_id "
        f"WHERE {' AND '.join(where)}"
    )

    params_page = list(params_sql)
    params_page.extend([limit, offset])
    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params_page))
        total_rows = await db.execute_fetchall(total_sql, tuple(params_sql))

    total_count = int(total_rows[0]["c"]) if total_rows else 0
    face_rows = [
        {
            "face_id": int(r["face_id"]),
            "media_id": int(r["media_id"]),
            "cluster_id": (int(r["cluster_id"]) if r["cluster_id"] is not None else None),
            "file_path": r["file_path"],
            "date_taken": r["date_taken"],
            "detection_conf": float(r["detection_conf"] or 0.0),
        }
        for r in rows
        if r["thumbnail_path"]
    ]
    return face_rows, total_count


def _unnamed_faces_empty_payload(use_last_results: bool) -> dict:
    return {
        "reply_text": "I couldn't find unnamed faces in those photos." if use_last_results else "I couldn't find unnamed faces right now.",
        "results": [],
        "face_results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "No unnamed faces found",
    }


def _unnamed_faces_success_payload(face_rows: list[dict], total_count: int, offset: int) -> dict:
    if total_count > len(face_rows):
        reply = f"I found {total_count} unnamed face thumbnails. Showing {offset + 1}-{offset + len(face_rows)}."
    else:
        suffix = "s" if len(face_rows) != 1 else ""
        reply = f"I found {len(face_rows)} unnamed face thumbnail{suffix}."

    return {
        "reply_text": reply,
        "results": [],
        "face_results": face_rows,
        "face_total_count": total_count,
        "count": total_count,
        "action": "none",
        "action_payload": {
            "offset": offset,
            "next_offset": (offset + len(face_rows)) if (offset + len(face_rows) < total_count) else None,
            "has_more": (offset + len(face_rows) < total_count),
        },
        "intent": "SQL_ONLY",
        "explanation": "Deterministic unnamed-face retrieval via AssistantV2 tool",
    }


async def tool_show_unnamed_faces(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    use_last_results = bool(params.get("use_last_results"))
    media_scope, scope_error_payload = _build_unnamed_faces_scope(ctx, params)
    if scope_error_payload is not None:
        return ToolExecutionResult(payload=scope_error_payload, next_state=ctx.state, notes="missing_last_results_scope")

    face_rows, total_count = await _query_unnamed_faces(media_scope, ctx.limit, ctx.offset)

    if not face_rows:
        payload = _unnamed_faces_empty_payload(use_last_results)
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="no_unnamed_faces")

    next_state = ctx.state.model_copy(deep=True)
    next_state.last_media_ids = list(dict.fromkeys(int(item["media_id"]) for item in face_rows))
    payload = _unnamed_faces_success_payload(face_rows, total_count, ctx.offset)
    return ToolExecutionResult(payload=payload, next_state=next_state, notes="deterministic_unnamed_faces")


async def tool_list_best_friends(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    person = str(params.get("person") or "").strip()
    if not person:
        payload = {
            "reply_text": "Whose best friends should I check?",
            "results": [],
            "count": 0,
            "action": "needs_clarification",
            "action_payload": {"type": "person_required", "example": "Who is Akshat most photographed with?"},
            "intent": "SQL_ONLY",
            "explanation": "Person missing for best-friends query",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="missing_person")

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT person_b, shared_photo_count
            FROM v_person_cooccurrence_named
            WHERE LOWER(person_a) = LOWER(?)
            ORDER BY shared_photo_count DESC
            LIMIT ?
            """,
            (person, min(max(ctx.limit, 1), 20)),
        )

    if not rows:
        next_state = ctx.state.model_copy(deep=True)
        next_state.last_people = [person]
        payload = {
            "reply_text": f"I couldn't find co-occurrence data for {person}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
            "intent": "SQL_ONLY",
            "explanation": "No co-occurrence rows",
        }
        return ToolExecutionResult(payload=payload, next_state=next_state, notes="no_best_friend_data")

    lines = [f"Top people most photographed with {person}:"]
    for idx, row in enumerate(rows, 1):
        lines.append(f"{idx}. {row['person_b']} ({row['shared_photo_count']} shared photos)")

    next_state = ctx.state.model_copy(deep=True)
    next_state.last_people = [person]
    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": len(rows),
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic best-friends retrieval via AssistantV2 tool",
    }
    return ToolExecutionResult(payload=payload, next_state=next_state, notes="deterministic_best_friends")


async def tool_list_locations(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    person = str(params.get("person") or "").strip()
    if not person:
        payload = {
            "reply_text": "Whose locations should I list?",
            "results": [],
            "count": 0,
            "action": "needs_clarification",
            "action_payload": {"type": "person_required", "example": "List all locations for Akshat"},
            "intent": "SQL_ONLY",
            "explanation": "Person missing for locations query",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="missing_person")

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT l.place_label, COUNT(DISTINCT l.media_id) AS photo_count
            FROM v_photos_with_location l
            JOIN v_person_photos p ON p.media_id = l.media_id
            WHERE LOWER(p.person_name) = LOWER(?)
              AND l.place_label IS NOT NULL
              AND l.place_label != ''
            GROUP BY l.place_label
            ORDER BY photo_count DESC, l.place_label COLLATE NOCASE ASC
            LIMIT 200
            """,
            (person,),
        )

    if not rows:
        next_state = ctx.state.model_copy(deep=True)
        next_state.last_people = [person]
        payload = {
            "reply_text": f"I couldn't find tagged locations for {person}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
            "intent": "SQL_ONLY",
            "explanation": "No location rows",
        }
        return ToolExecutionResult(payload=payload, next_state=next_state, notes="no_locations")

    preview = ", ".join(f"{str(r['place_label'])} ({int(r['photo_count'])})" for r in rows[:15])
    suffix = "" if len(rows) <= 15 else f" (+{len(rows)-15} more)"
    next_state = ctx.state.model_copy(deep=True)
    next_state.last_people = [person]
    next_state.last_location_term = str(rows[0]["place_label"])
    payload = {
        "reply_text": f"Top locations for {person} ({len(rows)} total): {preview}{suffix}",
        "results": [],
        "count": len(rows),
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic locations retrieval via AssistantV2 tool",
    }
    return ToolExecutionResult(payload=payload, next_state=next_state, notes="deterministic_locations")


def _extract_subject_person_from_with_location_query(message: str, state: AssistantState) -> str | None:
    text = (message or "").strip()
    if not text:
        return state.last_people[0] if state.last_people else None

    if re.search(r"\b(him|her|them|their)\b", text, flags=re.IGNORECASE):
        return state.last_people[0] if state.last_people else None

    m = re.search(r"\bwith\s+([\w]+(?:\s+[\w]+){0,10})\s+\b(?:in|from|near|at)\b", text, flags=re.IGNORECASE)
    if not m:
        return state.last_people[0] if state.last_people else None

    candidate = _clean_person_token(m.group(1))
    return candidate or (state.last_people[0] if state.last_people else None)


def _extract_year_from_message(message: str) -> int | None:
    m = re.search(r"\b(19\d{2}|20\d{2})\b", message or "")
    if not m:
        return None
    return int(m.group(1))


def _strip_year_suffix_from_location(location_term: str, year: int | None) -> str:
    if not location_term:
        return location_term
    if year is None:
        return location_term.strip()
    return re.sub(rf"\s+in\s+{year}\b", "", location_term, flags=re.IGNORECASE).strip()


def _is_pronoun_placeholder(value: str) -> bool:
    lowered = value.strip().lower().strip("<>")
    return lowered in {"him", "her", "them", "their", "his", "its", "person"}


async def _resolve_subject_person_for_location_query(ctx: ToolContext, params: dict[str, Any], message: str) -> str:
    person_param = _clean_person_token(str(params.get("person") or "").strip())
    if person_param and not _is_pronoun_placeholder(person_param):
        resolved = await _resolve_person_name(person_param)
        return resolved or person_param

    inferred = _extract_subject_person_from_with_location_query(message, ctx.state) or ""
    if inferred:
        resolved = await _resolve_person_name(inferred)
        return resolved or inferred
    return ""


def _resolve_year_for_location_query(params: dict[str, Any], message: str) -> int | None:
    # Guard against planner hallucinating/sticking a year from previous turns.
    # Only apply year filtering when the current user message explicitly contains a year.
    _ = params
    return _extract_year_from_message(message)


def _resolve_location_for_location_query(ctx: ToolContext, params: dict[str, Any], year: int | None) -> str:
    location_param = str(params.get("location_term") or "").strip()
    location_term = location_param or _effective_location_term(params) or ctx.state.last_location_term or ""
    return _strip_year_suffix_from_location(location_term, year)


def _missing_person_location_payload(kind: str) -> dict[str, Any]:
    if kind == "person":
        return {
            "reply_text": "Whose co-appearing people should I check?",
            "results": [],
            "count": 0,
            "action": "needs_clarification",
            "action_payload": {"type": "person_required", "example": "Who was with Akshat in Tomaree National Park?"},
            "intent": "SQL_ONLY",
            "explanation": "Person missing for location co-presence query",
        }
    return {
        "reply_text": "Which location should I use?",
        "results": [],
        "count": 0,
        "action": "needs_clarification",
        "action_payload": {"type": "location_required", "example": "Who was with Akshat in Tomaree National Park?"},
        "intent": "SQL_ONLY",
        "explanation": "Location missing for location co-presence query",
    }


async def tool_list_people_with_person_in_location(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    message = str(params.get("message") or "").strip()
    person = await _resolve_subject_person_for_location_query(ctx, params, message)
    year = _resolve_year_for_location_query(params, message)
    location_term = _resolve_location_for_location_query(ctx, params, year)

    if not person:
        payload = _missing_person_location_payload("person")
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="missing_person_for_location_copresence")

    if not location_term:
        payload = _missing_person_location_payload("location")
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="missing_location_for_location_copresence")

    sql = (
        "SELECT p2.person_name, COUNT(DISTINCT l.media_id) AS shared_photo_count "
        "FROM v_photos_with_location l "
        "JOIN v_person_photos p1 ON p1.media_id = l.media_id "
        "JOIN v_person_photos p2 ON p2.media_id = l.media_id "
        "WHERE LOWER(p1.person_name) = LOWER(?) "
        "AND LOWER(p2.person_name) != LOWER(?) "
        "AND LOWER(l.place_label) LIKE LOWER(?)"
    )
    params_sql: list[object] = [person, person, f"%{location_term}%"]
    if year is not None:
        sql += " AND CAST(strftime('%Y', l.date_taken) AS INTEGER) = ?"
        params_sql.append(year)
    sql += (
        " GROUP BY p2.person_name "
        "ORDER BY shared_photo_count DESC, p2.person_name COLLATE NOCASE ASC "
        "LIMIT ?"
    )
    params_sql.append(max(1, min(ctx.limit, 50)))

    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params_sql))

    if not rows:
        suffix = f" in {year}" if year is not None else ""
        payload = {
            "reply_text": f"I couldn't find people with {person} in {location_term}{suffix}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
            "intent": "SQL_ONLY",
            "explanation": "No co-presence rows for person/location filter",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="no_people_with_person_in_location")

    lines = [f"People with {person} in {location_term}{f' ({year})' if year is not None else ''}:"]
    for idx, row in enumerate(rows, 1):
        lines.append(f"{idx}. {row['person_name']} ({row['shared_photo_count']} photos)")

    next_state = ctx.state.model_copy(deep=True)
    next_state.last_people = [person]
    next_state.last_location_term = location_term
    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": len(rows),
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic co-presence by person/location filter",
    }
    return ToolExecutionResult(payload=payload, next_state=next_state, notes="deterministic_people_with_person_in_location")


async def tool_list_other_people_in_last_results(ctx: ToolContext, _params: dict[str, Any]) -> ToolExecutionResult:
    media_ids = ctx.state.last_media_ids[:200]
    if not media_ids:
        payload = {
            "reply_text": "I don't have previous photo results yet. Ask me to show photos first, then I can list who else is in them.",
            "results": [],
            "count": 0,
            "action": "needs_clarification",
            "action_payload": {"type": "last_results_required", "example": "Show me photos of Simon"},
            "intent": "SQL_ONLY",
            "explanation": "Need previous result set for follow-up co-presence query",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="missing_last_results")

    include_limit = max(1, min(ctx.limit, 50))
    placeholders = ",".join("?" * len(media_ids))
    exclude_people = [p.strip() for p in ctx.state.last_people if p.strip()]

    sql = (
        "SELECT person_name, COUNT(DISTINCT media_id) AS shared_photo_count "
        "FROM v_person_photos "
        f"WHERE media_id IN ({placeholders}) "
        "AND person_name IS NOT NULL "
        "AND person_name != ''"
    )
    params_sql: list[object] = list(media_ids)
    if exclude_people:
        exclusion = _SQL_AND.join("LOWER(person_name) != LOWER(?)" for _ in exclude_people)
        sql += f" AND ({exclusion})"
        params_sql.extend(exclude_people)
    sql += " GROUP BY person_name ORDER BY shared_photo_count DESC, person_name COLLATE NOCASE ASC LIMIT ?"
    params_sql.append(include_limit)

    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params_sql))

    if not rows:
        payload = {
            "reply_text": "I couldn't find additional named people in these photos.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
            "intent": "SQL_ONLY",
            "explanation": "No co-appearing named people in current result set",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="no_other_people_in_last_results")

    lines = ["Other people appearing in these photos:"]
    for idx, row in enumerate(rows, 1):
        lines.append(f"{idx}. {row['person_name']} ({row['shared_photo_count']} photos)")

    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": len(rows),
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic co-presence over previous result set",
    }
    return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="deterministic_other_people_last_results")


def _extract_min_other_people_from_message(message: str) -> int | None:
    m = re.search(r"at\s+least\s+(\d+)\s+(?:other\s+)?people", message, flags=re.IGNORECASE)
    if not m:
        return None
    return max(0, min(int(m.group(1)), 100))


def _clean_person_token(raw: str) -> str:
    token = str(raw or "").strip().strip("\"'").strip()
    token = token.rstrip("?.!,")
    token = re.sub(r"\bat\s+least\s+\d+\s+(?:other\s+)?people\b", "", token, flags=re.IGNORECASE).strip()
    token = re.sub(r"\b\d+\s+(?:more\s+)?people\b", "", token, flags=re.IGNORECASE).strip()
    token = re.sub(r"\b(?:more\s+people|other\s+people|people|person)\b", "", token, flags=re.IGNORECASE).strip()
    token = re.sub(r"\s{2,}", " ", token).strip()
    return token


def _extract_people_from_message(message: str, state: AssistantState) -> list[str]:
    text = str(message or "").strip()
    if not text:
        return []

    m_pronoun_with = re.search(r"\b(?:his|her|their)\s+photos?.*?\bwith\s+(.+)$", text, flags=re.IGNORECASE)
    if m_pronoun_with and state.last_people:
        tail = m_pronoun_with.group(1).strip().rstrip("?.!")
        tail = re.split(r"\s+from\s+|\s+in\s+|\s+near\s+|\s+at\s+", tail, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        extras = [_clean_person_token(p) for p in re.split(r"\s+and\s+|\s*,\s*", tail, flags=re.IGNORECASE)]
        merged = [state.last_people[0], *[p for p in extras if p]]
        if merged:
            return merged

    m_pos_with = re.search(rf"{_POSSESSIVE_NAME}'s\s+photos?.*?\bwith\s+(.+)$", text, flags=re.IGNORECASE)
    if m_pos_with:
        first = _clean_person_token(m_pos_with.group(1))
        tail = m_pos_with.group(2).strip().rstrip("?.!")
        tail = re.split(r"\s+from\s+|\s+in\s+|\s+near\s+|\s+at\s+", tail, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        others = [_clean_person_token(p) for p in re.split(r"\s+and\s+|\s*,\s*", tail, flags=re.IGNORECASE)]
        people = [p for p in [first, *others] if p]
        if people:
            return people

    parsed = AssistantPlanner._extract_people(text, state)
    parsed = [_clean_person_token(p) for p in parsed]
    parsed = [p for p in parsed if p]
    if parsed:
        return parsed

    m_pos = re.search(rf"{_POSSESSIVE_NAME}'s\s+photos?", text, flags=re.IGNORECASE)
    if m_pos:
        person = _clean_person_token(m_pos.group(1))
        if person:
            return [person]

    if state.last_people:
        return [p for p in state.last_people if p]
    return []


async def _resolve_person_name(candidate: str) -> str | None:
    value = _clean_person_token(candidate)
    if not value:
        return None

    async with get_db() as db:
        exact = await db.execute_fetchall(
            """
            SELECT name
            FROM persons
            WHERE name IS NOT NULL
              AND name != ''
              AND is_merged = 0
              AND is_ignored = 0
              AND LOWER(name) = LOWER(?)
            LIMIT 1
            """,
            (value,),
        )
        if exact:
            return str(exact[0]["name"])

        rows = await db.execute_fetchall(
            """
            SELECT p.name, COALESCE(pc.photo_count, 0) AS photo_count
            FROM persons p
            LEFT JOIN v_person_photo_count pc ON pc.person_id = p.id
            WHERE p.name IS NOT NULL
              AND p.name != ''
              AND p.is_merged = 0
              AND p.is_ignored = 0
              AND LOWER(p.name) LIKE LOWER(?)
            ORDER BY photo_count DESC, LENGTH(p.name) ASC
            LIMIT 25
            """,
            (f"%{value}%",),
        )

    if not rows:
        return value

    lowered = value.lower()

    def _score(name: str, photo_count: int) -> tuple[int, int, int]:
        n = name.lower()
        if n == lowered:
            return (0, -photo_count, len(name))
        if n.startswith(lowered + " "):
            return (1, -photo_count, len(name))
        if f" {lowered} " in f" {n} ":
            return (2, -photo_count, len(name))
        if n.startswith(lowered):
            return (3, -photo_count, len(name))
        return (4, -photo_count, len(name))

    best = min(rows, key=lambda r: _score(str(r["name"]), int(r["photo_count"] or 0)))
    return str(best["name"])


async def _normalized_people(params: dict[str, Any], state: AssistantState) -> list[str]:
    candidates: list[str] = []

    people_raw = params.get("people")
    if isinstance(people_raw, list):
        candidates.extend(str(p).strip() for p in people_raw if str(p).strip())

    person_single = str(params.get("person") or "").strip()
    if person_single:
        candidates.append(person_single)

    message = str(params.get("message") or "").strip()
    if message:
        candidates.extend(_extract_people_from_message(message, state))

    if not candidates and state.last_people:
        candidates.extend([p for p in state.last_people if p])

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = await _resolve_person_name(candidate)
        if not resolved:
            continue
        key = resolved.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(resolved)
    return normalized


def _effective_min_other_people(params: dict[str, Any]) -> int | None:
    raw = params.get("min_other_people")
    if isinstance(raw, int):
        return max(0, min(raw, 100))
    if isinstance(raw, str) and raw.strip().isdigit():
        return max(0, min(int(raw.strip()), 100))
    message = str(params.get("message") or "")
    return _extract_min_other_people_from_message(message)


def _extract_location_term_from_message(message: str) -> str | None:
    text = str(message or "").strip()
    if not text:
        return None
    patterns = [
        r"\bfrom\s+([^?.!,]+)",
        r"\bin\s+([^?.!,]+)",
        r"\bnear\s+([^?.!,]+)",
        r"\bby\s+the\s+([^?.!,]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if not m:
            continue
        raw = m.group(1).strip().strip("\"'")
        raw = re.split(r"\s+(?:with|and|where|that|which)\b", raw, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if raw:
            return raw
    return None


def _effective_location_term(params: dict[str, Any]) -> str | None:
    direct = str(params.get("location_term") or "").strip()
    if direct:
        return direct
    return _extract_location_term_from_message(str(params.get("message") or ""))


async def _count_photos_for_people(
    people: list[str],
    min_other_people: int | None = None,
    location_term: str | None = None,
) -> int:
    if not people:
        return 0
    aliases = [f"p{i}" for i in range(len(people))]
    joins = [
        f"JOIN v_person_photos {aliases[i]} ON {aliases[i]}.media_id = {aliases[0]}.media_id"
        for i in range(1, len(aliases))
    ]
    where = _SQL_AND.join(f"LOWER({alias}.person_name) = LOWER(?)" for alias in aliases)
    sql = (
        f"SELECT COUNT(DISTINCT {aliases[0]}.media_id) AS c "
        f"FROM v_person_photos {aliases[0]} "
        f"{' '.join(joins)} "
        f"WHERE {where}"
    )
    if min_other_people is not None:
        min_people_total = len(people) + min_other_people
        sql += (
            f" AND {aliases[0]}.media_id IN ("
            "SELECT media_id FROM v_photo_persons_agg WHERE person_count >= ?"
            ")"
        )
        people = [*people, min_people_total]
    if location_term:
        sql += (
            f" AND {aliases[0]}.media_id IN ("
            "SELECT media_id FROM v_photos_with_location "
            "WHERE place_label IS NOT NULL AND place_label != '' AND LOWER(place_label) LIKE LOWER(?)"
            ")"
        )
        people = [*people, f"%{location_term}%"]
    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(people))
    return int(rows[0]["c"]) if rows else 0


async def _media_ids_for_people(
    people: list[str],
    limit: int,
    offset: int,
    min_other_people: int | None = None,
    location_term: str | None = None,
) -> list[int]:
    if not people:
        return []
    aliases = [f"p{i}" for i in range(len(people))]
    joins = [
        f"JOIN v_person_photos {aliases[i]} ON {aliases[i]}.media_id = {aliases[0]}.media_id"
        for i in range(1, len(aliases))
    ]
    where = _SQL_AND.join(f"LOWER({alias}.person_name) = LOWER(?)" for alias in aliases)
    params: list[object] = list(people)
    min_people_total = len(people) + min_other_people if min_other_people is not None else None
    extra_where = ""
    if min_people_total is not None:
        extra_where = (
            f" AND {aliases[0]}.media_id IN ("
            "SELECT media_id FROM v_photo_persons_agg WHERE person_count >= ?"
            ")"
        )
        params.append(min_people_total)
    if location_term:
        extra_where += (
            f" AND {aliases[0]}.media_id IN ("
            "SELECT media_id FROM v_photos_with_location "
            "WHERE place_label IS NOT NULL AND place_label != '' AND LOWER(place_label) LIKE LOWER(?)"
            ")"
        )
        params.append(f"%{location_term}%")
    params.extend([limit, offset])
    sql = (
        f"SELECT DISTINCT {aliases[0]}.media_id AS media_id, {aliases[0]}.date_taken AS date_taken "
        f"FROM v_person_photos {aliases[0]} "
        f"{' '.join(joins)} "
        f"WHERE {where}{extra_where} "
        "ORDER BY date_taken DESC "
        "LIMIT ? OFFSET ?"
    )
    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params))
    return [int(r["media_id"]) for r in rows]


async def tool_count_photos_of_people(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    people = await _normalized_people(params, ctx.state)
    min_other_people = _effective_min_other_people(params)
    location_term = _effective_location_term(params)
    if not people:
        payload = {
            "reply_text": "Whose photos should I count?",
            "results": [],
            "count": 0,
            "action": "needs_clarification",
            "action_payload": {"type": "people_required", "example": "How many photos of Akshat with Aditi?"},
            "intent": "SQL_ONLY",
            "explanation": "People missing for photo-count query",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="missing_people")

    total = await _count_photos_for_people(people, min_other_people=min_other_people, location_term=location_term)
    if len(people) == 1:
        reply = f"I found {total} photo{'s' if total != 1 else ''} of {people[0]}."
    else:
        joined = ", ".join(people[:-1]) + f" and {people[-1]}"
        reply = f"I found {total} photo{'s' if total != 1 else ''} where {joined} appear together."
    if min_other_people is not None:
        reply += f" (with at least {min_other_people} more people)"
    if location_term:
        reply += f" in {location_term}"

    next_state = ctx.state.model_copy(deep=True)
    next_state.last_people = people
    if location_term:
        next_state.last_location_term = location_term
    payload = {
        "reply_text": reply,
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic count photos of people via AssistantV2 tool",
    }
    return ToolExecutionResult(payload=payload, next_state=next_state, notes="deterministic_people_photo_count")


async def tool_show_photos_of_people(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    people = await _normalized_people(params, ctx.state)
    min_other_people = _effective_min_other_people(params)
    location_term = _effective_location_term(params)
    if not people:
        payload = {
            "reply_text": "Whose photos should I show?",
            "results": [],
            "count": 0,
            "action": "needs_clarification",
            "action_payload": {"type": "people_required", "example": "Show photos of Akshat with Aditi"},
            "intent": "SQL_ONLY",
            "explanation": "People missing for show-photos query",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="missing_people")

    total = await _count_photos_for_people(people, min_other_people=min_other_people, location_term=location_term)
    media_ids = await _media_ids_for_people(
        people,
        ctx.limit,
        ctx.offset,
        min_other_people=min_other_people,
        location_term=location_term,
    )
    if not media_ids:
        constraint_hint = (
            f" for {', '.join(people)} with at least {min_other_people} more people"
            if min_other_people is not None
            else ""
        )
        if location_term:
            constraint_hint += f" in {location_term}"
        payload = {
            "reply_text": f"I couldn't find exact matches{constraint_hint}. You can refine the query or try broader wording.",
            "results": [],
            "count": 0,
            "action": "open_search",
            "action_payload": {"query": str(params.get("message") or f"show photos of {' with '.join(people)}")},
            "intent": "SQL_ONLY",
            "explanation": "No deterministic people-photo matches",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="no_people_photo_matches")

    results = await _hydrate_media_rows(media_ids, sql_matched_ids=set(media_ids))
    next_state = ctx.state.model_copy(deep=True)
    next_state.last_people = people
    next_state.last_media_ids = media_ids
    if location_term:
        next_state.last_location_term = location_term
    payload = {
        "reply_text": (
            f"I found {total} matching photo{'s' if total != 1 else ''}. "
            f"Showing {ctx.offset + 1}-{ctx.offset + len(results)}."
            + (f" (Each has at least {min_other_people} more people.)" if min_other_people is not None else "")
            + (f" (Filtered to {location_term}.)" if location_term else "")
        ),
        "results": results,
        "count": total,
        "action": "open_search",
        "action_payload": {
            "query": str(params.get("message") or f"show photos of {' with '.join(people)}"),
            "offset": ctx.offset,
            "next_offset": (ctx.offset + len(results)) if (ctx.offset + len(results) < total) else None,
            "has_more": (ctx.offset + len(results) < total),
        },
        "intent": "SQL_ONLY",
        "explanation": "Deterministic show photos of people via AssistantV2 tool",
    }
    return ToolExecutionResult(payload=payload, next_state=next_state, notes="deterministic_people_photo_show")


async def tool_list_common_contacts(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    person_a = str(params.get("person_a") or "").strip()
    person_b = str(params.get("person_b") or "").strip()
    if not person_a or not person_b:
        payload = {
            "reply_text": "Please provide two people, for example: common contacts between Akshat and Aditi.",
            "results": [],
            "count": 0,
            "action": "needs_clarification",
            "action_payload": {"type": "two_people_required"},
            "intent": "SQL_ONLY",
            "explanation": "Missing person pair for common contacts",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="missing_person_pair")

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            WITH a AS (
                SELECT person_b AS contact, shared_photo_count AS cnt
                FROM v_person_cooccurrence_named
                WHERE LOWER(person_a) = LOWER(?)
            ),
            b AS (
                SELECT person_b AS contact, shared_photo_count AS cnt
                FROM v_person_cooccurrence_named
                WHERE LOWER(person_a) = LOWER(?)
            )
            SELECT a.contact, a.cnt AS with_a, b.cnt AS with_b, (a.cnt + b.cnt) AS score
            FROM a
            JOIN b ON b.contact = a.contact
            WHERE LOWER(a.contact) != LOWER(?)
              AND LOWER(a.contact) != LOWER(?)
            ORDER BY score DESC
            LIMIT ?
            """,
            (person_a, person_b, person_a, person_b, min(max(ctx.limit, 1), 30)),
        )

    if not rows:
        payload = {
            "reply_text": f"I couldn't find common contacts between {person_a} and {person_b}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
            "intent": "SQL_ONLY",
            "explanation": "No common contacts rows",
        }
        return ToolExecutionResult(payload=payload, next_state=ctx.state, notes="no_common_contacts")

    lines = [f"Common contacts between {person_a} and {person_b}:"]
    for idx, row in enumerate(rows, 1):
        lines.append(f"{idx}. {row['contact']} (with {person_a}: {row['with_a']}, with {person_b}: {row['with_b']})")

    next_state = ctx.state.model_copy(deep=True)
    next_state.last_people = [person_a, person_b]
    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": len(rows),
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic common contacts retrieval via AssistantV2 tool",
    }
    return ToolExecutionResult(payload=payload, next_state=next_state, notes="deterministic_common_contacts")


async def tool_natural_search(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    query = str(params.get("query") or "").strip()
    if not query:
        return ToolExecutionResult(
            payload={
                "reply_text": "Ask me anything about your photos.",
                "results": [],
                "count": 0,
                "action": "none",
                "action_payload": {},
                "intent": "SQL_ONLY",
                "explanation": "Empty query",
            },
            next_state=ctx.state,
        )

    raw = await natural_search(NaturalSearchRequest(query=query, limit=ctx.limit))
    results = raw.get("results", [])
    count = int(raw.get("count", 0) or 0)
    if count:
        reply_text = f"I found {count} matching photo{'s' if count != 1 else ''}. Results are loaded below."
    else:
        reply_text = "I couldn't find exact matches. You can refine the query."

    next_state = ctx.state.model_copy(deep=True)
    if results:
        next_state.last_media_ids = [int(r["media_id"]) for r in results if "media_id" in r]

    payload = {
        "reply_text": reply_text,
        "results": results,
        "count": count,
        "action": "open_search",
        "action_payload": {"query": query},
        "intent": raw.get("intent"),
        "explanation": raw.get("explanation"),
        "error": raw.get("error"),
    }
    return ToolExecutionResult(payload=payload, next_state=next_state, notes="natural_search")


async def _metadata_branch_hits(
    query: str,
    limit: int,
    categories: tuple[str, ...],
) -> tuple[list[int], dict[int, float]]:
    tokens = [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", query or "")
        if len(token) >= 3
    ]
    if not tokens:
        return [], {}

    where_tokens = " OR ".join("LOWER(label) LIKE LOWER(?)" for _ in tokens)
    category_placeholders = ",".join("?" * len(categories))
    params_sql: list[object] = [f"%{token}%" for token in tokens]
    params_sql.extend(categories)
    params_sql.append(max(limit * 8, 80))

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""
            SELECT media_file_id, category, label
            FROM media_tags
            WHERE ({where_tokens})
              AND category IN ({category_placeholders})
            ORDER BY media_file_id DESC
            LIMIT ?
            """,
            tuple(params_sql),
        )

    scores: dict[int, float] = {}
    for row in rows:
        media_id = int(row["media_file_id"])
        label = str(row["label"] or "").lower()
        category = str(row["category"] or "")
        matched_tokens = sum(1 for token in tokens if token in label)
        if matched_tokens == 0:
            continue
        base = _metadata_category_weight(category)
        scores[media_id] = scores.get(media_id, 0.0) + (base * matched_tokens)

    ranked_ids = sorted(scores, key=scores.get, reverse=True)[:limit]
    return ranked_ids, scores


async def _face_branch_hits(
    message: str,
    state: AssistantState,
    limit: int,
) -> tuple[list[int], dict[int, float], list[str]]:
    people = await _normalized_people({"message": message}, state)
    if not people:
        return [], {}, []

    location_term = _extract_location_term_from_message(message)
    min_other_people = _extract_min_other_people_from_message(message)
    media_ids = await _media_ids_for_people(
        people,
        limit=max(limit * 3, 60),
        offset=0,
        min_other_people=min_other_people,
        location_term=location_term,
    )
    scores = {
        media_id: max(0.4, 2.4 - (rank * 0.04))
        for rank, media_id in enumerate(media_ids)
    }
    return media_ids[:limit], scores, people


def _accumulate_branch_scores(
    fused_scores: dict[int, float],
    branch_scores: dict[int, float],
    source_hits: dict[int, list[str]],
    branch_name: str,
) -> None:
    for media_id, score in branch_scores.items():
        fused_scores[media_id] = fused_scores.get(media_id, 0.0) + score
        source_hits.setdefault(media_id, []).append(branch_name)


def _metadata_category_weight(category: str) -> float:
    if category == "ocr":
        return 1.35
    if category in {"caption", "region"}:
        return 1.15
    return 0.8


def _scale_branch_scores(scores: dict[int, float], multiplier: float) -> dict[int, float]:
    if multiplier == 1.0:
        return scores
    return {media_id: (score * multiplier) for media_id, score in scores.items()}


def _is_text_heavy_query(message: str) -> bool:
    text = (message or "").lower()
    if not text:
        return False
    text_cues = (
        "ocr",
        "text",
        "caption",
        "description",
        "described",
        "says",
        "written",
        "read",
        "reads",
        "contains",
        "phrase",
        "word",
        "sentence",
        "invoice",
        "receipt",
        "sign",
        "board",
        "poster",
        "document",
        "license",
        "plate",
    )
    return any(cue in text for cue in text_cues)


def _broker_branch_multipliers(message: str) -> dict[str, float]:
    # Default balance works well for mixed multimodal queries.
    multipliers = {
        "natural_search": 1.0,
        "metadata_text": 1.0,
        "face_lookup": 1.0,
        "ocr_lookup": 1.0,
    }

    # Text-heavy intent: prioritize Florence-derived text channels.
    if _is_text_heavy_query(message):
        multipliers["natural_search"] = 0.8
        multipliers["face_lookup"] = 0.9
        multipliers["metadata_text"] = 1.35
        multipliers["ocr_lookup"] = 1.65

    return multipliers


def _normalize_broker_branches(raw_branches: Any) -> list[str]:
    candidates = (
        raw_branches
        if isinstance(raw_branches, list)
        else ["natural_search", "metadata_text", "face_lookup", "ocr_lookup"]
    )
    branch_order = [
        branch
        for branch in candidates
        if branch in {"natural_search", "metadata_text", "face_lookup", "ocr_lookup"}
    ]
    return branch_order or ["natural_search", "metadata_text", "face_lookup", "ocr_lookup"]


def _natural_branch_score_map(natural: dict[str, Any]) -> tuple[dict[int, float], set[int], list[str]]:
    natural_results = natural.get("results") or []
    natural_ids = [int(item["media_id"]) for item in natural_results if item.get("media_id") is not None]
    natural_scores = {
        media_id: max(0.35, 1.8 - (rank * 0.03))
        for rank, media_id in enumerate(natural_ids)
    }
    explanations = [str(natural["explanation"])] if natural.get("explanation") else []
    return natural_scores, set(natural_ids), explanations


def _fallback_natural_ids(natural: dict[str, Any] | None, limit: int) -> list[int]:
    if not isinstance(natural, dict):
        return []
    return [
        int(item["media_id"])
        for item in (natural.get("results") or [])
        if item.get("media_id") is not None
    ][:limit]


def _broker_reply_text(result_count: int) -> str:
    if result_count:
        suffix = "es" if result_count != 1 else ""
        return f"I found {result_count} hybrid match{suffix}. Results are loaded below."
    return "I couldn't find a strong multimodal match yet. Try adding a person, place, activity, or text clue."


def _build_broker_tasks(
    message: str,
    state: AssistantState,
    branch_order: list[str],
    candidate_limit: int,
) -> dict[str, Awaitable[Any]]:
    tasks: dict[str, Awaitable[Any]] = {}
    if "natural_search" in branch_order:
        tasks["natural_search"] = natural_search(NaturalSearchRequest(query=message, limit=candidate_limit))
    if "metadata_text" in branch_order:
        tasks["metadata_text"] = _metadata_branch_hits(
            message,
            candidate_limit,
            ("object", "animal", "geography", "place", "caption", "region"),
        )
    if "face_lookup" in branch_order:
        tasks["face_lookup"] = _face_branch_hits(message, state, candidate_limit)
    if "ocr_lookup" in branch_order:
        tasks["ocr_lookup"] = _metadata_branch_hits(message, candidate_limit, ("ocr",))
    return tasks


def _merge_broker_results(
    gathered: dict[str, Any],
    branch_multipliers: dict[str, float] | None = None,
) -> tuple[dict[int, float], set[int], list[str], list[str]]:
    branch_multipliers = branch_multipliers or {}
    fused_scores: dict[int, float] = {}
    source_hits: dict[int, list[str]] = {}
    sql_matched_ids: set[int] = set()
    resolved_people: list[str] = []
    explanations: list[str] = []

    natural = gathered.get("natural_search")
    if isinstance(natural, dict):
        natural_scores, natural_ids, natural_explanations = _natural_branch_score_map(natural)
        natural_scores = _scale_branch_scores(
            natural_scores,
            float(branch_multipliers.get("natural_search", 1.0)),
        )
        sql_matched_ids.update(natural_ids)
        _accumulate_branch_scores(fused_scores, natural_scores, source_hits, "natural_search")
        explanations.extend(natural_explanations)

    metadata = gathered.get("metadata_text")
    if isinstance(metadata, tuple):
        _, metadata_scores = metadata
        metadata_scores = _scale_branch_scores(
            metadata_scores,
            float(branch_multipliers.get("metadata_text", 1.0)),
        )
        _accumulate_branch_scores(fused_scores, metadata_scores, source_hits, "metadata_text")

    face = gathered.get("face_lookup")
    if isinstance(face, tuple):
        _, face_scores, resolved_people = face
        face_scores = _scale_branch_scores(
            face_scores,
            float(branch_multipliers.get("face_lookup", 1.0)),
        )
        _accumulate_branch_scores(fused_scores, face_scores, source_hits, "face_lookup")

    ocr = gathered.get("ocr_lookup")
    if isinstance(ocr, tuple):
        _, ocr_scores = ocr
        ocr_scores = _scale_branch_scores(
            ocr_scores,
            float(branch_multipliers.get("ocr_lookup", 1.0)),
        )
        _accumulate_branch_scores(fused_scores, ocr_scores, source_hits, "ocr_lookup")

    ranked_ids = sorted(
        fused_scores,
        key=lambda media_id: (fused_scores[media_id], len(source_hits.get(media_id, []))),
        reverse=True,
    )
    return ranked_ids, sql_matched_ids, resolved_people, explanations


async def tool_retrieval_broker(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    message = str(params.get("message") or params.get("query") or "").strip()
    if not message:
        return ToolExecutionResult(
            payload={
                "reply_text": "Ask me anything about your photos.",
                "results": [],
                "count": 0,
                "action": "none",
                "action_payload": {},
                "intent": "HYBRID_RETRIEVAL",
                "explanation": "Empty broker query",
            },
            next_state=ctx.state,
            notes="retrieval_broker_empty",
        )

    branch_order = _normalize_broker_branches(params.get("retrieval_branches"))
    candidate_limit = max(ctx.limit * 2, min(settings.hybrid_retrieval_candidate_limit, 200))
    tasks = _build_broker_tasks(message, ctx.state, branch_order, candidate_limit)

    branch_names = list(tasks)
    branch_results = await asyncio.gather(*(tasks[name] for name in branch_names))
    gathered = dict(zip(branch_names, branch_results, strict=False))
    branch_multipliers = _broker_branch_multipliers(message)
    ranked_ids, sql_matched_ids, resolved_people, explanations = _merge_broker_results(
        gathered,
        branch_multipliers=branch_multipliers,
    )
    ranked_ids = ranked_ids[:ctx.limit]

    if not ranked_ids:
        ranked_ids = _fallback_natural_ids(gathered.get("natural_search"), ctx.limit)

    results = await _hydrate_media_rows(ranked_ids, sql_matched_ids=sql_matched_ids) if ranked_ids else []
    next_state = ctx.state.model_copy(deep=True)
    next_state.last_media_ids = ranked_ids
    if resolved_people:
        next_state.last_people = resolved_people
    location_term = _extract_location_term_from_message(message)
    if location_term:
        next_state.last_location_term = location_term

    reply_text = _broker_reply_text(len(results))

    explanation_parts = []
    if explanations:
        explanation_parts.append(explanations[0])
    explanation_parts.append(f"Broker branches: {', '.join(branch_order)}")
    if _is_text_heavy_query(message):
        explanation_parts.append(
            "Adaptive weighting: boosted metadata_text/ocr_lookup for text-centric query"
        )
    payload = {
        "reply_text": reply_text,
        "results": results,
        "count": len(results),
        "action": "open_search",
        "action_payload": {"query": message, "broker": True, "branches": branch_order},
        "intent": "HYBRID_RETRIEVAL",
        "explanation": "; ".join(explanation_parts),
    }
    return ToolExecutionResult(payload=payload, next_state=next_state, notes=f"retrieval_broker:{','.join(branch_order)}")


async def tool_sql_agent(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    message = str(params.get("message") or "").strip()
    if not message:
        return _sql_agent_no_query_result(ctx)

    sql, reason = await _plan_sql_agent_query(message, ctx.state)
    if not sql:
        return _sql_agent_plan_failed_result(ctx, reason)

    payload_rows, exec_error = await _execute_sql_agent_query(sql, ctx.limit, ctx.offset)
    if exec_error:
        repaired_sql, repair_reason = await _repair_sql_agent_query(message, sql, exec_error)
        if repaired_sql:
            payload_rows, repair_error = await _execute_sql_agent_query(repaired_sql, ctx.limit, ctx.offset)
            if repair_error is None and payload_rows is not None:
                return await _sql_agent_result_from_rows(ctx, message, payload_rows, f"{reason};repair:{repair_reason}")
        return ToolExecutionResult(
            payload={
                "reply_text": "I couldn't execute a safe database query for that yet. Try a more specific prompt (entity + metric + scope).",
                "results": [],
                "count": 0,
                "action": "needs_clarification",
                "action_payload": {
                    "type": "sql_execution_failed",
                    "example": "Top 10 people by photo_count from v_person_photo_count",
                },
                "intent": "SQL_ONLY",
                "explanation": "SQL agent execution failed",
            },
            next_state=ctx.state,
            notes=f"sql_agent_exec_failed:{exec_error}",
        )

    assert payload_rows is not None
    return await _sql_agent_result_from_rows(ctx, message, payload_rows, reason)


async def tool_legacy_assistant(ctx: ToolContext, params: dict[str, Any]) -> ToolExecutionResult:
    message = str(params.get("message") or "").strip()
    planner = AssistantPlanner()
    plan = await planner.plan(message, ctx.state, ctx.limit)
    plan.offset = ctx.offset
    executed = await execute_plan(plan, ctx.state, ctx.limit)
    return ToolExecutionResult(
        payload=executed.payload,
        next_state=executed.state,
        notes=f"legacy_op:{plan.operation}",
    )


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="count_indexed_photos",
            description="Count all active indexed photos.",
            read_only=True,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=tool_count_indexed_photos,
        )
    )
    registry.register(
        ToolSpec(
            name="count_named_people",
            description="Count unique named people.",
            read_only=True,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=tool_count_named_people,
        )
    )
    registry.register(
        ToolSpec(
            name="count_named_faces",
            description="Count faces mapped to named people.",
            read_only=True,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=tool_count_named_faces,
        )
    )
    registry.register(
        ToolSpec(
            name="show_unnamed_faces",
            description="Return unnamed face thumbnails, optionally scoped to previous result set.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {"use_last_results": {"type": "boolean"}},
                "additionalProperties": False,
            },
            handler=tool_show_unnamed_faces,
        )
    )
    registry.register(
        ToolSpec(
            name="count_photos_of_people",
            description="Count photos where one or more people appear together.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {
                    "people": {"type": "array", "items": {"type": "string"}},
                    "person": {"type": "string"},
                    "message": {"type": "string"},
                    "location_term": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=tool_count_photos_of_people,
        )
    )
    registry.register(
        ToolSpec(
            name="show_photos_of_people",
            description="Show photos where one or more people appear together, optionally filtered by location.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {
                    "people": {"type": "array", "items": {"type": "string"}},
                    "person": {"type": "string"},
                    "message": {"type": "string"},
                    "location_term": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=tool_show_photos_of_people,
        )
    )
    registry.register(
        ToolSpec(
            name="list_best_friends",
            description="List people most frequently photographed with a person.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {"person": {"type": "string"}},
                "required": ["person"],
                "additionalProperties": False,
            },
            handler=tool_list_best_friends,
        )
    )
    registry.register(
        ToolSpec(
            name="list_common_contacts",
            description="List common contacts between two people.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {
                    "person_a": {"type": "string"},
                    "person_b": {"type": "string"},
                },
                "required": ["person_a", "person_b"],
                "additionalProperties": False,
            },
            handler=tool_list_common_contacts,
        )
    )
    registry.register(
        ToolSpec(
            name="list_locations",
            description="List known tagged locations for a person.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {"person": {"type": "string"}},
                "required": ["person"],
                "additionalProperties": False,
            },
            handler=tool_list_locations,
        )
    )
    registry.register(
        ToolSpec(
            name="list_people_with_person_in_location",
            description="List people who appear with a person in a specific location, optionally in a specific year.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {
                    "person": {"type": "string"},
                    "location_term": {"type": "string"},
                    "year": {"type": "integer"},
                    "message": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=tool_list_people_with_person_in_location,
        )
    )
    registry.register(
        ToolSpec(
            name="list_other_people_in_last_results",
            description="List people appearing in the previous photo result set, excluding current focus person(s).",
            read_only=True,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=tool_list_other_people_in_last_results,
        )
    )
    registry.register(
        ToolSpec(
            name="natural_search",
            description="Run SQL/CLIP hybrid natural search for a photo query.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=tool_natural_search,
        )
    )
    registry.register(
        ToolSpec(
            name="retrieval_broker",
            description="Run hybrid multimodal retrieval by fusing natural search, metadata text, face lookup, and OCR evidence.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "query": {"type": "string"},
                    "retrieval_branches": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["natural_search", "metadata_text", "face_lookup", "ocr_lookup"],
                        },
                    },
                },
                "required": ["message"],
                "additionalProperties": False,
            },
            handler=tool_retrieval_broker,
        )
    )
    registry.register(
        ToolSpec(
            name="sql_agent",
            description="Open-ended read-only SQL analytics over curated VIP views for broad database questions.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            handler=tool_sql_agent,
        )
    )
    registry.register(
        ToolSpec(
            name="legacy_assistant",
            description="Bridge to AssistantV1 planner+executor for compatibility.",
            read_only=True,
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            handler=tool_legacy_assistant,
        )
    )
    return registry
