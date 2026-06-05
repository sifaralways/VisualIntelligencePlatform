from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from backend.assistant.types import AssistantPlan, AssistantState, PendingPersonClarification
from backend.api.routes.search import NaturalSearchRequest, natural_search, _hydrate_media_rows
from backend.database.db import get_db


SQL_AND = " AND "
PERSON_JOINER = " and "
EXACT_PEOPLE_COUNT_CLAUSE = " AND agg.person_count = ?"
PERSON_SELECTION_SPLIT = r"\s*,\s*|\s+and\s+"
PERSON_SCOPE_STOPWORDS = {
    "show",
    "me",
    "all",
    "unnamed",
    "unidentified",
    "unknown",
    "face",
    "faces",
    "photo",
    "photos",
    "in",
    "of",
    "with",
    "from",
    "these",
    "those",
}


def _query_has_explicit_people_scope(query: str) -> bool:
    q = (query or "").lower()
    return bool(re.search(r"\b(of|with|between)\b", q) or re.search(r"\b[a-z]+'s\s+photos?\b", q))


def _query_has_explicit_location_scope(query: str) -> bool:
    q = (query or "").lower()
    return bool(re.search(r"\b(from|in|near|at|by\s+the)\b", q))


def _contextualize_fallback_query(query: str, state: AssistantState) -> str:
    q = (query or "").strip()
    if not q:
        return q

    contextual = q
    if state.last_people and not _query_has_explicit_people_scope(q):
        contextual = f"{contextual} for {PERSON_JOINER.join(state.last_people)}"
    if state.last_location_term and ("there" in q.lower() or "that place" in q.lower() or not _query_has_explicit_location_scope(q)):
        contextual = f"{contextual} in {state.last_location_term}"
    return contextual


def _extract_location_term_from_query(query: str) -> str | None:
    text = (query or "").strip()
    # Person-possessive scopes like "from Gordon's photos" are not locations.
    if re.search(r"\bfrom\s+[a-z]+(?:\s+[a-z]+){0,8}['’]s\s+photos?\b", text, flags=re.IGNORECASE):
        return None
    if re.search(r"\b[a-z]+(?:\s+[a-z]+){0,2}['’]s\s+photos?\b", text, flags=re.IGNORECASE):
        return None

    # People scopes like "in photos with Gordon" are not locations.
    if re.search(r"\b(?:in|from)\s+photos?\s+with\b", text, flags=re.IGNORECASE):
        return None

    # Deictic follow-up scopes are not literal places.
    if re.search(
        r"\b(?:in|from)\s+(?:these|those|the\s+current|current|above|prior|previous)\s+(?:photos?|results?)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return None

    patterns = [
        r"\bfrom\s+([^?.!,]+)",
        r"\bin\s+([^?.!,]+)",
        r"\bnear\s+([^?.!,]+)",
        r"\bat\s+([^?.!,]+)",
        r"\bby\s+the\s+([^?.!,]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        value = m.group(1).strip().strip("'\"")
        value = re.split(r"\s+(?:with|and|where|that|which)\b", value, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if value.lower() in {"photo", "photos", "result", "results", "image", "images", "picture", "pictures"}:
            continue
        if value.lower() in {"these photos", "those photos", "these results", "those results", "current results", "current photos"}:
            continue
        if value:
            return value
    return None


def _clean_person_candidate(text: str) -> str:
    tokens = [t for t in re.split(r"\s+", (text or "").strip()) if t]
    while tokens and tokens[0].lower() in PERSON_SCOPE_STOPWORDS:
        tokens.pop(0)
    while tokens and tokens[-1].lower() in PERSON_SCOPE_STOPWORDS:
        tokens.pop()
    return " ".join(tokens).strip("'\" ")


def _normalized_person_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _person_equals_sql(column: str) -> str:
    return f"LOWER({column}) = LOWER(?)"


def _person_not_equals_sql(column: str) -> str:
    return f"LOWER({column}) != LOWER(?)"


def _person_tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9']+", (text or "").lower()) if token]


async def _find_person_candidates(raw_name: str, limit: int = 8) -> list[str]:
    cleaned = _clean_person_candidate(raw_name)
    tokens = _person_tokens(cleaned)
    if not cleaned or not tokens:
        return []

    token_clause = SQL_AND.join("LOWER(name) LIKE ?" for _ in tokens)
    params: list[object] = [f"%{token}%" for token in tokens]
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""
            SELECT DISTINCT name
            FROM persons
            WHERE name IS NOT NULL
              AND name != ''
              AND is_merged = 0
              AND is_ignored = 0
              AND {token_clause}
            LIMIT 100
            """,
            tuple(params),
        )

    candidates = [str(row["name"]) for row in rows]
    raw_key = _normalized_person_key(cleaned)

    def score(name: str) -> tuple[int, int, str]:
        key = _normalized_person_key(name)
        if key == raw_key:
            priority = 0
        elif key.startswith(f"{raw_key} "):
            priority = 1
        elif raw_key in key:
            priority = 2
        else:
            priority = 3
        return (priority, len(name), key)

    return sorted(candidates, key=score)[:limit]


async def _resolve_person_name(
    raw_name: str,
    plan: AssistantPlan,
    field_name: str,
    field_index: int | None,
    state: AssistantState,
) -> tuple[str | None, PendingPersonClarification | None]:
    cleaned = _clean_person_candidate(raw_name)
    if not cleaned:
        return raw_name, None

    candidates = await _find_person_candidates(cleaned)
    if not candidates:
        return raw_name, None

    exact_matches = [candidate for candidate in candidates if _normalized_person_key(candidate) == _normalized_person_key(cleaned)]
    if len(exact_matches) == 1:
        return exact_matches[0], None
    if len(candidates) == 1:
        return candidates[0], None

    pending = PendingPersonClarification(
        original_message=state.last_user_query or plan.query or "",
        original_plan=plan.model_copy(deep=True),
        field_name=field_name,
        field_index=field_index,
        requested_name=cleaned,
        candidate_names=candidates,
    )
    return None, pending


async def _resolve_plan_people(
    plan: AssistantPlan,
    state: AssistantState,
) -> tuple[AssistantPlan, PendingPersonClarification | None]:
    resolved = plan.model_copy(deep=True)

    for field_name in ("person", "person_a", "person_b"):
        value = getattr(resolved, field_name)
        if not value:
            continue
        exact_name, pending = await _resolve_person_name(value, resolved, field_name, None, state)
        if pending is not None:
            return resolved, pending
        setattr(resolved, field_name, exact_name)

    for index, raw_name in enumerate(list(resolved.people)):
        if not raw_name:
            continue
        exact_name, pending = await _resolve_person_name(raw_name, resolved, "people", index, state)
        if pending is not None:
            return resolved, pending
        resolved.people[index] = exact_name or raw_name

    return resolved, None


def _build_person_clarification_payload(pending: PendingPersonClarification, invalid: bool = False) -> dict:
    intro = "Please reply with one number or exact name to continue:" if invalid else "Please reply with one number or exact name so I can continue:"
    lines = [f"I found multiple matches for '{pending.requested_name}'.", intro]
    for index, candidate in enumerate(pending.candidate_names, 1):
        lines.append(f"{index}. {candidate}")
    return {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": 0,
        "action": "needs_clarification",
        "action_payload": {
            "type": "person_selection",
            "requested_name": pending.requested_name,
            "candidates": pending.candidate_names,
        },
        "intent": "SQL_ONLY",
        "explanation": "Ambiguous person name requires clarification",
    }


def _parse_person_clarification_selection(message: str, pending: PendingPersonClarification) -> str | None:
    text = (message or "").strip()
    if not text:
        return None

    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(pending.candidate_names):
            return pending.candidate_names[index - 1]

    parts = [part.strip() for part in re.split(PERSON_SELECTION_SPLIT, text, flags=re.IGNORECASE) if part.strip()]
    if len(parts) == 1 and parts[0].isdigit():
        index = int(parts[0])
        if 1 <= index <= len(pending.candidate_names):
            return pending.candidate_names[index - 1]

    normalized_map = {_normalized_person_key(candidate): candidate for candidate in pending.candidate_names}
    normalized_text = _normalized_person_key(text)
    if normalized_text in normalized_map:
        return normalized_map[normalized_text]

    for part in parts:
        normalized_part = _normalized_person_key(part)
        if normalized_part in normalized_map:
            return normalized_map[normalized_part]

    return None


def _apply_person_clarification(plan: AssistantPlan, pending: PendingPersonClarification, selected_name: str) -> AssistantPlan:
    updated = plan.model_copy(deep=True)
    if pending.field_name == "people":
        if pending.field_index is None or pending.field_index >= len(updated.people):
            return updated
        updated.people[pending.field_index] = selected_name
        return updated

    setattr(updated, pending.field_name, selected_name)
    return updated


async def continue_pending_person_clarification(
    message: str,
    state: AssistantState,
    default_limit: int,
) -> tuple[ExecutionResult, AssistantPlan] | None:
    pending = state.pending_person_clarification
    if pending is None:
        return None

    selected_name = _parse_person_clarification_selection(message, pending)
    if selected_name is None:
        next_state = state.model_copy(deep=True)
        next_state.pending_person_clarification = pending
        return ExecutionResult(payload=_build_person_clarification_payload(pending, invalid=True), state=next_state), pending.original_plan

    clarified_plan = _apply_person_clarification(pending.original_plan, pending, selected_name)
    next_state = state.model_copy(deep=True)
    next_state.pending_person_clarification = None
    executed = await execute_plan(clarified_plan, next_state, default_limit)
    executed.state.pending_person_clarification = None
    return executed, clarified_plan


@dataclass
class ExecutionResult:
    payload: dict
    state: AssistantState


def _bounded_limit(plan: AssistantPlan, default_limit: int) -> int:
    lim = int(plan.limit or default_limit)
    return max(1, min(lim, 200))


async def execute_plan(plan: AssistantPlan, state: AssistantState, default_limit: int) -> ExecutionResult:
    limit = _bounded_limit(plan, default_limit)
    plan, pending = await _resolve_plan_people(plan, state)
    if pending is not None:
        next_state = state.model_copy(deep=True)
        next_state.pending_person_clarification = pending
        return ExecutionResult(payload=_build_person_clarification_payload(pending), state=next_state)

    state = state.model_copy(deep=True)
    state.pending_person_clarification = None

    simple_handlers = {
        "LIST_CAPABILITIES": lambda: _list_capabilities(state),
        "SHOW_UNNAMED_FACES": lambda: _show_unnamed_faces(plan, state, limit),
        "COUNT_INDEXED_PHOTOS": lambda: _count_indexed_photos(state),
        "COUNT_NAMED_FACES": lambda: _count_named_faces(state),
        "COUNT_NAMED_PEOPLE": lambda: _count_named_people(state),
        "FOLLOWUP_SHOW_LAST_RESULTS": lambda: _followup_show_last_results(state),
        "COUNT_PHOTOS_OF_PEOPLE": lambda: _count_photos_of_people(plan, state),
        "COUNT_PEOPLE_WITH_PERSON": lambda: _count_people_with_person(plan, state),
        "SHOW_PHOTOS_OF_PEOPLE": lambda: _show_photos_of_people(plan, state, limit),
        "SHOW_PHOTOS_OF_PEOPLE_IN_LOCATION": lambda: _show_photos_of_people_in_location(plan, state, limit),
        "LIST_OTHER_PEOPLE_IN_PHOTOS_OF_PEOPLE": lambda: _list_other_people_in_photos_of_people(plan, state, limit),
        "LIST_OTHER_PEOPLE_IN_LAST_RESULTS": lambda: _list_other_people_in_last_results(state, limit),
        "LIST_BEST_FRIENDS": lambda: _list_best_friends(plan, state),
        "LIST_COMMON_CONTACTS": lambda: _list_common_contacts(plan, state),
        "LIST_LOCATIONS": lambda: _list_locations(plan, state),
        "LAST_LOCATION": lambda: _last_location(plan, state),
        "FIRST_LOCATION": lambda: _first_location(plan, state),
        "TIMELINE_LOCATIONS": lambda: _timeline_locations(plan, state),
        "LIST_PEOPLE_WITH_PERSON_IN_LOCATION_TIME": lambda: _list_people_with_person_in_location_time(plan, state),
        "LIST_LOCATIONS_FOR_LAST_RESULTS": lambda: _list_locations_for_last_results(state),
    }

    handler = simple_handlers.get(plan.operation)
    if handler is not None:
        return await handler()

    return await _natural_fallback(plan.query or state.last_user_query or "", state, limit)


async def _count_indexed_photos(state: AssistantState) -> ExecutionResult:
    async with get_db() as db:
        row = await db.execute_fetchall(
            """
            SELECT COUNT(*) AS c
            FROM media_files
            WHERE removed_from_app = 0
              AND is_stub = 0
            """
        )
    total = int(row[0]["c"])
    payload = {
        "reply_text": f"So far, {total} photos are indexed in VIP.",
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    return ExecutionResult(payload=payload, state=state)


async def _list_capabilities(state: AssistantState) -> ExecutionResult:
    await asyncio.sleep(0)
    payload = {
        "reply_text": (
            "I can help with photo-library questions and actions. Try commands like: "
            "'How many photos are indexed?', 'How many named faces?', 'Show photos of Akshat', "
            "'Show photos of Akshat with Aditi', 'Show photos of Akshat from India', "
            "'Show unnamed faces', 'Show unnamed faces in these photos', "
            "'Who is Akshat most photographed with?', 'Common contacts between Akshat and Aditi', "
            "'List all locations for Akshat', 'What is Akshat\'s first/last location?', and 'Show them' for follow-ups."
        ),
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    return ExecutionResult(payload=payload, state=state)


def _query_requests_last_results_scope(query: str) -> bool:
    lowered = (query or "").lower()
    return any(
        phrase in lowered
        for phrase in (
            "these photos",
            "those photos",
            "these results",
            "those results",
            "in these",
            "in those",
            "current results",
            "above photos",
        )
    )


def _clean_people_candidates(parts: list[str]) -> list[str]:
    people = [_clean_person_candidate(p.strip().strip("'\"")) for p in parts if p.strip()]
    return [p for p in people if p and p.lower() not in PERSON_SCOPE_STOPWORDS]


def _extract_people_from_relation_tail(text: str, relation: str) -> list[str]:
    m = re.search(rf"(?:faces?|photos?)\s+{relation}\s+(.+)$", text, flags=re.IGNORECASE)
    if not m:
        return []

    tail = m.group(1).strip().rstrip("?.!")
    tail = re.split(r"\s+from\s+|\s+in\s+|\s+near\s+|\s+at\s+", tail, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    split_pattern = r"\s+and\s+|\s*,\s*" if relation == "with" else r"\s+with\s+|\s+and\s+|\s*,\s*"
    return _clean_people_candidates(re.split(split_pattern, tail, flags=re.IGNORECASE))


def _extract_people_scope_from_unnamed_faces_query(query: str, state: AssistantState) -> list[str]:
    text = (query or "").strip()

    # e.g. "from Gordon's photos", "in Gordon's photos", "Gordon's photos"
    possessive_patterns = (
        r"\bfrom\s+([a-z]+(?:\s+[a-z]+){0,8})['’]s\s+photos?\b",
        r"\bin\s+([a-z]+(?:\s+[a-z]+){0,8})['’]s\s+photos?\b",
        r"\b([a-z]+(?:\s+[a-z]+){0,8})['’]s\s+photos?\b",
    )
    for pattern in possessive_patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if not m:
            continue
        person = _clean_person_candidate(m.group(1).strip().rstrip("?.!"))
        if person:
            return [person]

    # e.g. "unnamed faces with Gordon", "unnamed faces in photos with Gordon", "unnamed faces of Gordon and Naomi"
    for relation in ("with", "of"):
        people = _extract_people_from_relation_tail(text, relation)
        if people:
            return people

    # If user says "his/her/their photos", reuse previous person context.
    if re.search(r"\b(his|her|their)\s+photos?\b", text, flags=re.IGNORECASE) and state.last_people:
        return [state.last_people[0]]

    return []


def _people_scope_label(people: list[str]) -> str:
    return PERSON_JOINER.join(people)


def _empty_unnamed_faces_reply(scoped_people: list[str], scoped_to_last: bool) -> str:
    if scoped_people:
        return f"I couldn't find unnamed faces in {_people_scope_label(scoped_people)}'s photos."
    if scoped_to_last:
        return "I couldn't find unnamed faces in those photos."
    return "I couldn't find unnamed faces right now."


def _unnamed_faces_scope_text(scoped_people: list[str], scoped_to_last: bool) -> str:
    if scoped_people:
        return f" in {_people_scope_label(scoped_people)}'s photos"
    if scoped_to_last:
        return " in those photos"
    return ""


def _empty_unnamed_faces_payload(reply_text: str) -> dict:
    return {
        "reply_text": reply_text,
        "results": [],
        "face_results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }


def _build_unnamed_faces_found_reply(total_faces: int, shown_faces: int, scope_text: str) -> str:
    plural = "s" if total_faces != 1 else ""
    if total_faces > shown_faces:
        return f"I found {total_faces} unnamed face thumbnail{plural}{scope_text}. Showing {shown_faces}."
    return f"I found {shown_faces} unnamed face thumbnail{'s' if shown_faces != 1 else ''}{scope_text}."


async def _scoped_media_ids_for_unnamed_faces(
    scoped_people: list[str],
    scoped_location: str | None,
    base_scope_media_ids: list[int] | None,
) -> tuple[list[int] | None, str | None]:
    scope_ids = base_scope_media_ids

    if scoped_people:
        person_media_ids = await _media_ids_for_people(scoped_people, 2000)
        if not person_media_ids:
            names = _people_scope_label(scoped_people)
            return None, f"I couldn't find photos for {names} to search unnamed faces."

        if scope_ids is None:
            scope_ids = person_media_ids
        else:
            allowed = set(person_media_ids)
            scope_ids = [m for m in scope_ids if m in allowed]

    if scoped_location:
        location_media_ids = await _media_ids_for_location_term(scoped_location, 3000)
        if not location_media_ids:
            return None, f"I couldn't find photos in {scoped_location} to search unnamed faces."

        if scope_ids is None:
            scope_ids = location_media_ids
        else:
            allowed = set(location_media_ids)
            scope_ids = [m for m in scope_ids if m in allowed]

    return scope_ids, None


async def _unnamed_face_rows(limit: int, media_ids: list[int] | None = None) -> tuple[list[dict], int]:
    limit = max(1, min(limit, 500))
    params: list[object] = []
    where_parts = [
        "owner.id IS NULL",
        "mf.removed_from_app = 0",
        "mf.is_stub = 0",
    ]
    if media_ids:
        placeholders = ",".join("?" for _ in media_ids)
        where_parts.append(f"f.media_file_id IN ({placeholders})")
        params.extend(media_ids)

    params_with_limit = list(params)
    params_with_limit.append(limit)
    sql = (
        "SELECT f.id AS face_id, f.media_file_id AS media_id, f.cluster_id, f.thumbnail_path, "
        "mf.file_path, mf.date_taken, f.detection_conf "
        "FROM faces f "
        "JOIN media_files mf ON mf.id = f.media_file_id "
        "LEFT JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid "
        "LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid "
        "LEFT JOIN persons owner ON owner.person_guid = cpc.person_guid AND owner.is_merged = 0 AND owner.is_ignored = 0 "
        f"WHERE {' AND '.join(where_parts)} "
        "ORDER BY COALESCE(f.detection_conf, 0) DESC, f.id DESC "
        "LIMIT ?"
    )

    total_sql = (
        "SELECT COUNT(*) AS c "
        "FROM faces f "
        "JOIN media_files mf ON mf.id = f.media_file_id "
        "LEFT JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid "
        "LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid "
        "LEFT JOIN persons owner ON owner.person_guid = cpc.person_guid AND owner.is_merged = 0 AND owner.is_ignored = 0 "
        f"WHERE {' AND '.join(where_parts)}"
    )

    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params_with_limit))
        total_rows = await db.execute_fetchall(total_sql, tuple(params))

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


async def _show_unnamed_faces(plan: AssistantPlan, state: AssistantState, limit: int) -> ExecutionResult:
    query = (plan.query or state.last_user_query or "").strip()
    scoped_to_last = _query_requests_last_results_scope(query)
    scoped_people = _extract_people_scope_from_unnamed_faces_query(query, state)
    scoped_location = _extract_location_term_from_query(query)
    if not scoped_location and ("there" in query.lower() or "that place" in query.lower()):
        scoped_location = state.last_location_term

    if scoped_to_last and not state.last_media_ids:
        payload = _empty_unnamed_faces_payload(
            "I don't have previous photo results yet. Ask for photos first, then I can show unnamed faces in those photos."
        )
        return ExecutionResult(payload=payload, state=state)

    scope_media_ids: list[int] | None = state.last_media_ids[:1000] if scoped_to_last else None
    scope_media_ids, scope_error = await _scoped_media_ids_for_unnamed_faces(scoped_people, scoped_location, scope_media_ids)
    if scope_error:
        payload = _empty_unnamed_faces_payload(scope_error)
        return ExecutionResult(payload=payload, state=state)

    face_rows, total_faces = await _unnamed_face_rows(limit, scope_media_ids)
    if not face_rows:
        reply = _empty_unnamed_faces_reply(scoped_people, scoped_to_last)
        payload = _empty_unnamed_faces_payload(reply)
        return ExecutionResult(payload=payload, state=state)

    unique_media_ids = list(dict.fromkeys(int(r["media_id"]) for r in face_rows))
    new_state = state.model_copy(deep=True)
    new_state.last_media_ids = unique_media_ids
    if scoped_people:
        new_state.last_people = scoped_people
    if scoped_location:
        new_state.last_location_term = scoped_location
    new_state.last_operation = plan.operation

    scope_text = _unnamed_faces_scope_text(scoped_people, scoped_to_last)
    reply_text = _build_unnamed_faces_found_reply(total_faces, len(face_rows), scope_text)
    payload = {
        "reply_text": reply_text,
        "results": [],
        "face_results": face_rows,
        "face_total_count": total_faces,
        "count": total_faces,
        "action": "none",
        "action_payload": {},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic unnamed-face retrieval",
    }
    return ExecutionResult(payload=payload, state=new_state)


async def _count_named_faces(state: AssistantState) -> ExecutionResult:
    async with get_db() as db:
        identified = await db.execute_fetchall(
            """
            SELECT COUNT(*) AS c
            FROM faces f
            JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
            JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
            JOIN persons p ON p.person_guid = cpc.person_guid
            WHERE p.is_merged = 0
            """
        )
        named = await db.execute_fetchall(
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
    total_identified = int(identified[0]["c"])
    total_named = int(named[0]["c"])
    payload = {
        "reply_text": (
            f"So far, {total_named} out of {total_identified} identified faces "
            "are mapped to named people."
        ),
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    return ExecutionResult(payload=payload, state=state)


async def _count_named_people(state: AssistantState) -> ExecutionResult:
    async with get_db() as db:
        row = await db.execute_fetchall(
            """
            SELECT COUNT(*) AS c
            FROM persons
            WHERE name IS NOT NULL
              AND name != ''
              AND is_merged = 0
              AND is_ignored = 0
            """
        )
    total = int(row[0]["c"])
    payload = {
        "reply_text": f"So far, {total} unique people have been named.",
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    return ExecutionResult(payload=payload, state=state)


def _people_from_plan(plan: AssistantPlan, state: AssistantState) -> list[str]:
    if plan.people:
        return [p.strip() for p in plan.people if p.strip()]
    if plan.person:
        return [plan.person.strip()]
    return state.last_people


async def _count_photos_for_people(people: list[str], min_other_people: int | None = None, exact_people_only: bool = False) -> int:
    if not people:
        return 0

    aliases = [f"p{i}" for i in range(len(people))]
    joins = [
        f"JOIN v_person_photos {aliases[i]} ON {aliases[i]}.media_id = {aliases[0]}.media_id"
        for i in range(1, len(aliases))
    ]
    where = SQL_AND.join(_person_equals_sql(f"{alias}.person_name") for alias in aliases)
    params: list[object] = list(people)

    having_clause = ""
    if min_other_people is not None:
        having_clause = " AND agg.person_count >= ?"
        params.append(len(people) + int(min_other_people))
    elif exact_people_only:
        having_clause = EXACT_PEOPLE_COUNT_CLAUSE
        params.append(len(people))

    sql = (
        f"SELECT COUNT(DISTINCT {aliases[0]}.media_id) AS c "
        f"FROM v_person_photos {aliases[0]} "
        f"{' '.join(joins)} "
        f"JOIN v_photo_persons_agg agg ON agg.media_id = {aliases[0]}.media_id "
        f"WHERE {where}{having_clause}"
    )

    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params))
    return int(rows[0]["c"])


async def _media_ids_for_people(
    people: list[str],
    limit: int,
    min_other_people: int | None = None,
    exact_people_only: bool = False,
    offset: int = 0,
) -> list[int]:
    if not people:
        return []

    aliases = [f"p{i}" for i in range(len(people))]
    joins = [
        f"JOIN v_person_photos {aliases[i]} ON {aliases[i]}.media_id = {aliases[0]}.media_id"
        for i in range(1, len(aliases))
    ]
    where = SQL_AND.join(_person_equals_sql(f"{alias}.person_name") for alias in aliases)
    params: list[object] = list(people)

    people_clause = ""
    if min_other_people is not None:
        people_clause = " AND agg.person_count >= ?"
        params.append(len(people) + int(min_other_people))
    elif exact_people_only:
        people_clause = EXACT_PEOPLE_COUNT_CLAUSE
        params.append(len(people))

    params.append(limit)
    params.append(max(0, int(offset)))

    sql = (
        f"SELECT DISTINCT {aliases[0]}.media_id AS media_id, {aliases[0]}.date_taken AS date_taken "
        f"FROM v_person_photos {aliases[0]} "
        f"{' '.join(joins)} "
        f"JOIN v_photo_persons_agg agg ON agg.media_id = {aliases[0]}.media_id "
        f"WHERE {where}{people_clause} "
        "ORDER BY date_taken DESC "
        "LIMIT ? OFFSET ?"
    )

    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params))
    return [int(r["media_id"]) for r in rows]


async def _media_ids_for_people_in_location(
    people: list[str],
    location_term: str,
    limit: int,
    exact_people_only: bool = False,
    offset: int = 0,
) -> list[int]:
    if not people or not location_term.strip():
        return []

    aliases = [f"p{i}" for i in range(len(people))]
    joins = [
        f"JOIN v_person_photos {aliases[i]} ON {aliases[i]}.media_id = {aliases[0]}.media_id"
        for i in range(1, len(aliases))
    ]
    where_people = SQL_AND.join(_person_equals_sql(f"{alias}.person_name") for alias in aliases)

    params: list[object] = list(people)
    people_clause = ""
    if exact_people_only:
        people_clause = EXACT_PEOPLE_COUNT_CLAUSE
        params.append(len(people))
    params.append(f"%{location_term}%")
    params.append(limit)
    params.append(max(0, int(offset)))

    sql = (
        f"SELECT DISTINCT {aliases[0]}.media_id AS media_id, {aliases[0]}.date_taken AS date_taken "
        f"FROM v_person_photos {aliases[0]} "
        f"{' '.join(joins)} "
        f"JOIN v_photo_persons_agg agg ON agg.media_id = {aliases[0]}.media_id "
        f"JOIN v_photos_with_location l ON l.media_id = {aliases[0]}.media_id "
        f"WHERE {where_people}{people_clause} AND l.place_label LIKE ? "
        "ORDER BY date_taken DESC "
        "LIMIT ? OFFSET ?"
    )

    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params))
    return [int(r["media_id"]) for r in rows]


async def _count_photos_for_people_in_location(people: list[str], location_term: str, exact_people_only: bool = False) -> int:
    if not people or not location_term.strip():
        return 0

    aliases = [f"p{i}" for i in range(len(people))]
    joins = [
        f"JOIN v_person_photos {aliases[i]} ON {aliases[i]}.media_id = {aliases[0]}.media_id"
        for i in range(1, len(aliases))
    ]
    where_people = SQL_AND.join(_person_equals_sql(f"{alias}.person_name") for alias in aliases)
    params: list[object] = list(people)
    people_clause = ""
    if exact_people_only:
        people_clause = EXACT_PEOPLE_COUNT_CLAUSE
        params.append(len(people))
    params.append(f"%{location_term}%")

    sql = (
        f"SELECT COUNT(DISTINCT {aliases[0]}.media_id) AS c "
        f"FROM v_person_photos {aliases[0]} "
        f"{' '.join(joins)} "
        f"JOIN v_photo_persons_agg agg ON agg.media_id = {aliases[0]}.media_id "
        f"JOIN v_photos_with_location l ON l.media_id = {aliases[0]}.media_id "
        f"WHERE {where_people}{people_clause} AND l.place_label LIKE ?"
    )

    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params))
    return int(rows[0]["c"])


async def _media_ids_for_location_term(location_term: str, limit: int) -> list[int]:
    term = (location_term or "").strip()
    if not term:
        return []
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT DISTINCT media_id
            FROM v_photos_with_location
            WHERE place_label LIKE ?
              AND place_label IS NOT NULL
              AND place_label != ''
            ORDER BY date_taken DESC
            LIMIT ?
            """,
            (f"%{term}%", max(1, min(limit, 5000))),
        )
    return [int(r["media_id"]) for r in rows]


async def _count_photos_of_people(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    people = _people_from_plan(plan, state)
    if not people:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 100)

    total = await _count_photos_for_people(people, plan.min_other_people, plan.exact_people_only)
    reply = _build_people_photo_count_reply(total, people, plan.min_other_people)

    new_state = state.model_copy(deep=True)
    new_state.last_people = people
    new_state.last_operation = plan.operation
    payload = {
        "reply_text": reply,
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    return ExecutionResult(payload=payload, state=new_state)


def _build_people_photo_count_reply(total: int, people: list[str], min_other_people: int | None) -> str:
    suffix = "s" if total != 1 else ""
    if len(people) == 1:
        if min_other_people is None:
            return f"I found {total} photo{suffix} of {people[0]}."
        return f"I found {total} photo{suffix} of {people[0]} with at least {min_other_people} other people."

    names = ", ".join(people[:-1]) + f" and {people[-1]}"
    if min_other_people is None:
        return f"I found {total} photo{suffix} where {names} appear together."
    return f"I found {total} photo{suffix} where {names} appear together with at least {min_other_people} other people."


async def _show_photos_of_people(plan: AssistantPlan, state: AssistantState, limit: int) -> ExecutionResult:
    people = _people_from_plan(plan, state)
    if not people:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, limit)

    offset = max(0, int(plan.offset or 0))
    total = await _count_photos_for_people(people, plan.min_other_people, plan.exact_people_only)
    media_ids = await _media_ids_for_people(people, limit, plan.min_other_people, plan.exact_people_only, offset)
    if not media_ids:
        payload = {
            "reply_text": "I couldn't find exact matches. You can refine the query or try broader wording.",
            "results": [],
            "count": 0,
            "action": "open_search",
            "action_payload": {"query": plan.query or f"show photos of {' with '.join(people)}"},
        }
        return ExecutionResult(payload=payload, state=state)

    results = await _hydrate_media_rows(media_ids, sql_matched_ids=set(media_ids))
    new_state = state.model_copy(deep=True)
    new_state.last_people = people
    new_state.last_media_ids = media_ids
    new_state.last_operation = plan.operation
    payload = {
        "reply_text": f"I found {total} matching photo{'s' if total != 1 else ''}. Showing {offset + 1}-{offset + len(results)}.",
        "results": results,
        "count": total,
        "action": "open_search",
        "action_payload": {
            "query": plan.query or f"show photos of {' with '.join(people)}",
            "offset": offset,
            "next_offset": (offset + len(results)) if (offset + len(results) < total) else None,
            "has_more": (offset + len(results) < total),
        },
        "intent": "SQL_ONLY",
        "explanation": "Deterministic people-set retrieval",
    }
    return ExecutionResult(payload=payload, state=new_state)


async def _show_photos_of_people_in_location(plan: AssistantPlan, state: AssistantState, limit: int) -> ExecutionResult:
    people = _people_from_plan(plan, state)
    location_term = (plan.location_term or "").strip()
    if not people or not location_term:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, limit)

    offset = max(0, int(plan.offset or 0))
    total = await _count_photos_for_people_in_location(people, location_term, plan.exact_people_only)
    media_ids = await _media_ids_for_people_in_location(people, location_term, limit, plan.exact_people_only, offset)
    if not media_ids:
        subject = PERSON_JOINER.join(people)
        payload = {
            "reply_text": f"I couldn't find photos of {subject} from {location_term}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    results = await _hydrate_media_rows(media_ids, sql_matched_ids=set(media_ids))
    new_state = state.model_copy(deep=True)
    new_state.last_people = people
    new_state.last_location_term = location_term
    new_state.last_media_ids = media_ids
    new_state.last_operation = plan.operation
    payload = {
        "reply_text": f"I found {total} matching photo{'s' if total != 1 else ''} from {location_term}. Showing {offset + 1}-{offset + len(results)}.",
        "results": results,
        "count": total,
        "action": "open_search",
        "action_payload": {
            "query": plan.query or f"show photos of {' with '.join(people)} from {location_term}",
            "offset": offset,
            "next_offset": (offset + len(results)) if (offset + len(results) < total) else None,
            "has_more": (offset + len(results) < total),
        },
        "intent": "SQL_ONLY",
        "explanation": "Deterministic people+location retrieval",
    }
    return ExecutionResult(payload=payload, state=new_state)


async def _followup_show_last_results(state: AssistantState) -> ExecutionResult:
    if not state.last_media_ids:
        payload = {
            "reply_text": "I don't have previous results to show yet. Ask for photos first.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    media_ids = state.last_media_ids[:100]
    results = await _hydrate_media_rows(media_ids, sql_matched_ids=set(media_ids))
    payload = {
        "reply_text": f"I found {len(results)} matching photo{'s' if len(results) != 1 else ''}. Results are loaded below.",
        "results": results,
        "count": len(results),
        "action": "open_search",
        "action_payload": {"query": state.last_user_query or "show previous results"},
        "intent": "SQL_ONLY",
    }
    return ExecutionResult(payload=payload, state=state)


async def _list_best_friends(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    person = (plan.person or (plan.people[0] if plan.people else None) or (state.last_people[0] if state.last_people else None))
    if not person:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 50)

    limit = max(1, min(int(plan.limit or 10), 20))
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""
            SELECT person_b, shared_photo_count
            FROM v_person_cooccurrence_named
            WHERE {_person_equals_sql('person_a')}
            ORDER BY shared_photo_count DESC
            LIMIT ?
            """,
            (person, limit),
        )

    if not rows:
        payload = {
            "reply_text": f"I couldn't find co-occurrence data for {person}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    lines = [f"Top people most photographed with {person}:"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['person_b']} ({r['shared_photo_count']} shared photos)")

    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    new_state = state.model_copy(deep=True)
    new_state.last_people = [person]
    new_state.last_operation = plan.operation
    return ExecutionResult(payload=payload, state=new_state)


async def _count_people_with_person(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    person = plan.person or (plan.people[0] if plan.people else None) or (state.last_people[0] if state.last_people else None)
    if not person:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 50)

    async with get_db() as db:
        rows = await db.execute_fetchall(
                        f"""
            SELECT COUNT(*) AS c
            FROM v_person_cooccurrence_named
                        WHERE {_person_equals_sql('person_a')}
                            AND {_person_not_equals_sql('person_b')}
            """,
                        (person, person),
        )

    total = int(rows[0]["c"])
    payload = {
        "reply_text": f"{total} unique people have appeared with {person} in photos.",
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    new_state = state.model_copy(deep=True)
    new_state.last_people = [person]
    new_state.last_operation = plan.operation
    return ExecutionResult(payload=payload, state=new_state)


async def _list_other_people_in_photos_of_people(plan: AssistantPlan, state: AssistantState, limit: int) -> ExecutionResult:
    people = _people_from_plan(plan, state)
    if not people:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, limit)

    media_ids = await _media_ids_for_people(people, min(max(limit, 50), 200), plan.min_other_people)
    if not media_ids:
        payload = {
            "reply_text": "I couldn't find matching photos for that people combination.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    result = await _list_other_people_from_media_ids(media_ids, people, limit, state)
    result.state.last_media_ids = media_ids
    result.state.last_people = people
    result.state.last_operation = plan.operation
    return result


async def _list_other_people_in_last_results(state: AssistantState, limit: int) -> ExecutionResult:
    if not state.last_media_ids:
        payload = {
            "reply_text": "I don't have previous photo results yet. Ask for photos first.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    result = await _list_other_people_from_media_ids(state.last_media_ids[:200], state.last_people, limit, state)
    result.state.last_operation = "LIST_OTHER_PEOPLE_IN_LAST_RESULTS"
    return result


async def _list_other_people_from_media_ids(
    media_ids: list[int],
    base_people: list[str],
    limit: int,
    state: AssistantState,
) -> ExecutionResult:
    if not media_ids:
        payload = {
            "reply_text": "No photos available to inspect co-appearing people.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    include_limit = max(1, min(limit, 50))
    placeholders = ",".join("?" * len(media_ids))
    exclude_people = [p.strip() for p in base_people if p.strip()]
    exclude_clause = ""
    params: list[object] = list(media_ids)
    if exclude_people:
        not_like = SQL_AND.join(_person_not_equals_sql("person_name") for _ in exclude_people)
        exclude_clause = f"AND ({not_like})"
        params.extend(exclude_people)
    params.append(include_limit)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""
            SELECT person_name, COUNT(DISTINCT media_id) AS shared_photo_count
            FROM v_person_photos
            WHERE media_id IN ({placeholders})
              AND person_name IS NOT NULL
              AND person_name != ''
              {exclude_clause}
            GROUP BY person_name
            ORDER BY shared_photo_count DESC, person_name COLLATE NOCASE ASC
            LIMIT ?
            """,
            tuple(params),
        )

    if not rows:
        payload = {
            "reply_text": "I couldn't find additional people in that photo set.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    lines = ["Other people appearing in those photos:"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['person_name']} ({row['shared_photo_count']} photos)")

    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    new_state = state.model_copy(deep=True)
    new_state.last_media_ids = media_ids
    new_state.last_people = base_people
    return ExecutionResult(payload=payload, state=new_state)


async def _list_common_contacts(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    person_a = plan.person_a or (plan.people[0] if plan.people else None)
    person_b = plan.person_b or (plan.people[1] if len(plan.people) > 1 else None)
    if not person_a or not person_b:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 50)

    limit = max(1, min(int(plan.limit or 15), 30))
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""
            WITH a AS (
                SELECT person_b AS contact, shared_photo_count AS cnt
                FROM v_person_cooccurrence_named
                WHERE {_person_equals_sql('person_a')}
            ),
            b AS (
                SELECT person_b AS contact, shared_photo_count AS cnt
                FROM v_person_cooccurrence_named
                WHERE {_person_equals_sql('person_a')}
            )
            SELECT a.contact, a.cnt AS with_a, b.cnt AS with_b, (a.cnt + b.cnt) AS score
            FROM a
            JOIN b ON b.contact = a.contact
            WHERE {_person_not_equals_sql('a.contact')}
              AND {_person_not_equals_sql('a.contact')}
            ORDER BY score DESC
            LIMIT ?
            """,
            (person_a, person_b, person_a, person_b, limit),
        )

    if not rows:
        payload = {
            "reply_text": f"I couldn't find common contacts between {person_a} and {person_b}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    lines = [f"Common contacts between {person_a} and {person_b}:"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['contact']} (with {person_a}: {r['with_a']}, with {person_b}: {r['with_b']})")

    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    return ExecutionResult(payload=payload, state=state)


async def _list_locations(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    person = plan.person or (plan.people[0] if plan.people else None) or (state.last_people[0] if state.last_people else None)
    if not person:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 50)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""
            SELECT l.place_label, COUNT(DISTINCT l.media_id) AS photo_count
            FROM v_photos_with_location l
            JOIN v_person_photos p ON p.media_id = l.media_id
            WHERE {_person_equals_sql('p.person_name')}
              AND l.place_label IS NOT NULL
              AND l.place_label != ''
            GROUP BY l.place_label
            ORDER BY photo_count DESC, l.place_label COLLATE NOCASE ASC
            LIMIT 200
            """,
            (person,),
        )

        labels = [str(r["place_label"]) for r in rows]
    if not labels:
        payload = {
            "reply_text": f"I couldn't find tagged locations for {person}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    preview = ", ".join(f"{str(r['place_label'])} ({int(r['photo_count'])})" for r in rows[:15])
    suffix = "" if len(labels) <= 15 else f" (+{len(labels)-15} more)"
    payload = {
        "reply_text": f"Top locations for {person} ({len(labels)} total): {preview}{suffix}",
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    new_state = state.model_copy(deep=True)
    new_state.last_people = [person]
    if rows:
        new_state.last_location_term = str(rows[0]["place_label"])
    new_state.last_operation = plan.operation
    return ExecutionResult(payload=payload, state=new_state)


async def _last_location(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    person = plan.person or (plan.people[0] if plan.people else None) or (state.last_people[0] if state.last_people else None)
    if not person:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 50)

    async with get_db() as db:
        rows = await db.execute_fetchall(
                        f"""
            SELECT l.place_label, l.date_taken, l.media_id
            FROM v_photos_with_location l
            JOIN v_person_photos p ON p.media_id = l.media_id
                        WHERE {_person_equals_sql('p.person_name')}
              AND l.place_label IS NOT NULL
              AND l.place_label != ''
            ORDER BY l.date_taken DESC
            LIMIT 1
            """,
            (person,),
        )

    if not rows:
        payload = {
            "reply_text": f"I couldn't find a location history for {person}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    row = rows[0]
    place = str(row["place_label"])
    date_taken = str(row["date_taken"] or "")
    date_text = date_taken[:10] if date_taken else "unknown date"
    media_id = int(row["media_id"])
    hydrated = await _hydrate_media_rows([media_id], sql_matched_ids={media_id})
    new_state = state.model_copy(deep=True)
    new_state.last_people = [person]
    new_state.last_location_term = place
    new_state.last_media_ids = [media_id]
    new_state.last_operation = plan.operation
    payload = {
        "reply_text": f"{person}'s latest tagged location is {place} ({date_text}).",
        "results": hydrated,
        "count": len(hydrated),
        "action": "open_search",
        "action_payload": {"query": f"show photos of {person} in {place}"},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic location lookup",
    }
    return ExecutionResult(payload=payload, state=new_state)


async def _first_location(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    person = plan.person or (plan.people[0] if plan.people else None) or (state.last_people[0] if state.last_people else None)
    if not person:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 50)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""
            SELECT l.place_label, l.date_taken, l.media_id
            FROM v_photos_with_location l
            JOIN v_person_photos p ON p.media_id = l.media_id
            WHERE {_person_equals_sql('p.person_name')}
              AND l.place_label IS NOT NULL
              AND l.place_label != ''
            ORDER BY l.date_taken ASC
            LIMIT 1
            """,
            (person,),
        )

    if not rows:
        payload = {
            "reply_text": f"I couldn't find an earliest tagged location for {person}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    row = rows[0]
    place = str(row["place_label"])
    date_taken = str(row["date_taken"] or "")
    date_text = date_taken[:10] if date_taken else "unknown date"
    media_id = int(row["media_id"])
    hydrated = await _hydrate_media_rows([media_id], sql_matched_ids={media_id})
    new_state = state.model_copy(deep=True)
    new_state.last_people = [person]
    new_state.last_location_term = place
    new_state.last_media_ids = [media_id]
    new_state.last_operation = plan.operation
    payload = {
        "reply_text": f"{person}'s earliest tagged location is {place} ({date_text}).",
        "results": hydrated,
        "count": len(hydrated),
        "action": "open_search",
        "action_payload": {"query": f"show photos of {person} in {place}"},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic earliest location lookup",
    }
    return ExecutionResult(payload=payload, state=new_state)


async def _timeline_locations(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    person = plan.person or (plan.people[0] if plan.people else None) or (state.last_people[0] if state.last_people else None)
    if not person:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 50)

    limit = max(1, min(int(plan.limit or 24), 60))
    async with get_db() as db:
        rows = await db.execute_fetchall(
                        f"""
            SELECT substr(l.date_taken, 1, 7) AS ym,
                   GROUP_CONCAT(DISTINCT l.place_label) AS places,
                   COUNT(DISTINCT l.media_id) AS photo_count
            FROM v_photos_with_location l
            JOIN v_person_photos p ON p.media_id = l.media_id
                        WHERE {_person_equals_sql('p.person_name')}
              AND l.place_label IS NOT NULL
              AND l.place_label != ''
              AND l.date_taken IS NOT NULL
            GROUP BY ym
            ORDER BY ym DESC
            LIMIT ?
            """,
            (person, limit),
        )

    if not rows:
        payload = {
            "reply_text": f"I couldn't find a location timeline for {person}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    lines = [f"Location timeline for {person}:"]
    for row in rows:
        lines.append(f"- {row['ym']}: {row['places']} ({row['photo_count']} photos)")

    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    return ExecutionResult(payload=payload, state=state)


async def _list_people_with_person_in_location_time(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    person = plan.person or (state.last_people[0] if state.last_people else None)
    if not person or not plan.location_term or not plan.year:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 50)

    limit = max(1, min(int(plan.limit or 20), 50))
    async with get_db() as db:
        rows = await db.execute_fetchall(
                        f"""
            SELECT p2.person_name AS companion, COUNT(DISTINCT p2.media_id) AS shared_photo_count
            FROM v_person_photos p1
            JOIN v_person_photos p2 ON p2.media_id = p1.media_id
            JOIN v_photos_with_location l ON l.media_id = p1.media_id
                        WHERE {_person_equals_sql('p1.person_name')}
              AND p2.person_name IS NOT NULL
              AND p2.person_name != ''
                            AND {_person_not_equals_sql('p2.person_name')}
              AND l.place_label LIKE ?
              AND substr(l.date_taken, 1, 4) = ?
            GROUP BY p2.person_name
            ORDER BY shared_photo_count DESC, companion COLLATE NOCASE ASC
            LIMIT ?
            """,
            (person, person, f"%{plan.location_term}%", str(plan.year), limit),
        )

    if not rows:
        payload = {
            "reply_text": f"I couldn't find people photographed with {person} in {plan.location_term} during {plan.year}.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    lines = [f"People photographed with {person} in {plan.location_term} ({plan.year}):"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['companion']} ({row['shared_photo_count']} photos)")

    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    new_state = state.model_copy(deep=True)
    new_state.last_people = [person]
    new_state.last_location_term = plan.location_term
    new_state.last_operation = plan.operation
    return ExecutionResult(payload=payload, state=new_state)


async def _list_locations_for_last_results(state: AssistantState) -> ExecutionResult:
    if not state.last_media_ids:
        payload = {
            "reply_text": "I don't have previous photo results yet. Ask for photos first.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    media_ids = state.last_media_ids[:200]
    placeholders = ",".join("?" * len(media_ids))
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""
            SELECT place_label, COUNT(DISTINCT media_id) AS photo_count
            FROM v_photos_with_location
            WHERE media_id IN ({placeholders})
              AND place_label IS NOT NULL
              AND place_label != ''
            GROUP BY place_label
            ORDER BY photo_count DESC, place_label COLLATE NOCASE ASC
            LIMIT 50
            """,
            tuple(media_ids),
        )

    if not rows:
        payload = {
            "reply_text": "I couldn't find tagged locations for the current photo set.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        return ExecutionResult(payload=payload, state=state)

    lines = ["Locations found in the current photos:"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['place_label']} ({row['photo_count']} photos)")

    payload = {
        "reply_text": "\n".join(lines),
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    new_state = state.model_copy(deep=True)
    if rows:
        new_state.last_location_term = str(rows[0]["place_label"])
    new_state.last_operation = "LIST_LOCATIONS_FOR_LAST_RESULTS"
    return ExecutionResult(payload=payload, state=new_state)


async def _natural_fallback(query: str, state: AssistantState, limit: int) -> ExecutionResult:
    scoped_query = _contextualize_fallback_query(query, state)
    payload = await natural_search(NaturalSearchRequest(query=scoped_query, limit=limit))

    bridge = await _maybe_run_deterministic_bridge(query, state, limit, payload)
    if bridge is not None:
        return bridge

    results = payload.get("results", [])
    count = int(payload.get("count", 0) or 0)
    if count > 0:
        reply_text = f"I found {count} matching photo{'s' if count != 1 else ''}. Results are loaded below."
    else:
        reply_text = "I couldn't find exact matches. You can refine the query or try broader wording."
    out = {
        "reply_text": reply_text,
        "results": results,
        "count": count,
        "action": "open_search",
        "action_payload": {"query": scoped_query},
        "intent": payload.get("intent"),
        "explanation": payload.get("explanation"),
        "error": payload.get("error"),
    }
    new_state = state.model_copy(deep=True)
    if results:
        new_state.last_media_ids = [int(r["media_id"]) for r in results]
    explicit_location = _extract_location_term_from_query(query)
    if explicit_location:
        new_state.last_location_term = explicit_location
    return ExecutionResult(payload=out, state=new_state)


def _natural_confidence(payload: dict) -> float | None:
    raw = payload.get("confidence")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _should_try_deterministic_bridge(query: str, payload: dict) -> bool:
    if not query.strip():
        return False
    if (payload.get("intent") or "").upper() == "CLIP_ONLY":
        return False

    confidence = _natural_confidence(payload)
    fallback_used = bool(payload.get("fallback_used"))
    if not fallback_used and (confidence is None or confidence > 0.45):
        return False

    q = query.lower()
    explicit_people = _query_has_explicit_people_scope(query) or bool(re.search(r"\b[a-z]+(?:\s+[a-z]+){0,3}'s\b", q))
    count_intent = "how many" in q
    photo_intent = bool(re.search(r"\b(show|open|load|photos?)\b", q))
    return explicit_people and (count_intent or photo_intent)


def _normalize_people_for_bridge(people: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in people:
        cleaned = re.split(r"\s+(?:from|in|near|at)\s+", (raw or "").strip(), maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


async def _maybe_run_deterministic_bridge(query: str, state: AssistantState, limit: int, payload: dict) -> ExecutionResult | None:
    if not _should_try_deterministic_bridge(query, payload):
        return None

    from backend.assistant.planner import AssistantPlanner

    lowered = query.lower()
    people = AssistantPlanner._extract_people(query, state)
    if not people:
        primary = AssistantPlanner._extract_primary_person(query, state)
        people = [primary] if primary else []
    people = _normalize_people_for_bridge(people)
    if not people:
        return None

    location_term = AssistantPlanner._extract_location_term(query)
    min_other = AssistantPlanner._extract_min_other_people(lowered)

    if "how many" in lowered:
        if location_term:
            total = await _count_photos_for_people_in_location(people, location_term)
            names = PERSON_JOINER.join(people)
            payload = {
                "reply_text": f"I found {total} photo{'s' if total != 1 else ''} of {names} from {location_term}.",
                "results": [],
                "count": 0,
                "action": "none",
                "action_payload": {},
            }
            new_state = state.model_copy(deep=True)
            new_state.last_people = people
            new_state.last_location_term = location_term
            new_state.last_operation = "COUNT_PHOTOS_OF_PEOPLE"
            return ExecutionResult(payload=payload, state=new_state)

        plan = AssistantPlan(
            operation="COUNT_PHOTOS_OF_PEOPLE",
            people=people,
            min_other_people=min_other,
            query=query,
            limit=limit,
            explanation="Deterministic bridge count",
        )
        return await _count_photos_of_people(plan, state)

    if location_term:
        plan = AssistantPlan(
            operation="SHOW_PHOTOS_OF_PEOPLE_IN_LOCATION",
            people=people,
            location_term=location_term,
            query=query,
            limit=limit,
            explanation="Deterministic bridge people+location",
        )
        return await _show_photos_of_people_in_location(plan, state, limit)

    plan = AssistantPlan(
        operation="SHOW_PHOTOS_OF_PEOPLE",
        people=people,
        min_other_people=min_other,
        query=query,
        limit=limit,
        explanation="Deterministic bridge people",
    )
    return await _show_photos_of_people(plan, state, limit)
