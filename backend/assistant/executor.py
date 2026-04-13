from __future__ import annotations

from dataclasses import dataclass

from backend.assistant.types import AssistantPlan, AssistantState
from backend.api.routes.search import NaturalSearchRequest, natural_search, _hydrate_media_rows
from backend.database.db import get_db


SQL_AND = " AND "


@dataclass
class ExecutionResult:
    payload: dict
    state: AssistantState


def _bounded_limit(plan: AssistantPlan, default_limit: int) -> int:
    lim = int(plan.limit or default_limit)
    return max(1, min(lim, 200))


async def execute_plan(plan: AssistantPlan, state: AssistantState, default_limit: int) -> ExecutionResult:
    limit = _bounded_limit(plan, default_limit)

    simple_handlers = {
        "COUNT_INDEXED_PHOTOS": lambda: _count_indexed_photos(state),
        "COUNT_NAMED_FACES": lambda: _count_named_faces(state),
        "COUNT_NAMED_PEOPLE": lambda: _count_named_people(state),
        "FOLLOWUP_SHOW_LAST_RESULTS": lambda: _followup_show_last_results(state),
        "COUNT_PHOTOS_OF_PEOPLE": lambda: _count_photos_of_people(plan, state),
        "COUNT_PEOPLE_WITH_PERSON": lambda: _count_people_with_person(plan, state),
        "SHOW_PHOTOS_OF_PEOPLE": lambda: _show_photos_of_people(plan, state, limit),
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


async def _count_named_faces(state: AssistantState) -> ExecutionResult:
    async with get_db() as db:
        identified = await db.execute_fetchall(
            "SELECT COUNT(*) AS c FROM faces WHERE person_id IS NOT NULL"
        )
        named = await db.execute_fetchall(
            """
            SELECT COUNT(*) AS c
            FROM faces f
            JOIN persons p ON p.id = f.person_id
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


async def _count_photos_for_people(people: list[str], min_other_people: int | None = None) -> int:
    if not people:
        return 0

    aliases = [f"p{i}" for i in range(len(people))]
    joins = [
        f"JOIN v_person_photos {aliases[i]} ON {aliases[i]}.media_id = {aliases[0]}.media_id"
        for i in range(1, len(aliases))
    ]
    where = SQL_AND.join(f"{alias}.person_name LIKE ?" for alias in aliases)
    params: list[object] = [f"%{name}%" for name in people]

    having_clause = ""
    if min_other_people is not None:
        having_clause = " AND agg.person_count >= ?"
        params.append(len(people) + int(min_other_people))

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


async def _media_ids_for_people(people: list[str], limit: int, min_other_people: int | None = None) -> list[int]:
    if not people:
        return []

    aliases = [f"p{i}" for i in range(len(people))]
    joins = [
        f"JOIN v_person_photos {aliases[i]} ON {aliases[i]}.media_id = {aliases[0]}.media_id"
        for i in range(1, len(aliases))
    ]
    where = SQL_AND.join(f"{alias}.person_name LIKE ?" for alias in aliases)
    params: list[object] = [f"%{name}%" for name in people]

    people_clause = ""
    if min_other_people is not None:
        people_clause = " AND agg.person_count >= ?"
        params.append(len(people) + int(min_other_people))

    params.append(limit)

    sql = (
        f"SELECT DISTINCT {aliases[0]}.media_id AS media_id, {aliases[0]}.date_taken AS date_taken "
        f"FROM v_person_photos {aliases[0]} "
        f"{' '.join(joins)} "
        f"JOIN v_photo_persons_agg agg ON agg.media_id = {aliases[0]}.media_id "
        f"WHERE {where}{people_clause} "
        "ORDER BY date_taken DESC "
        "LIMIT ?"
    )

    async with get_db() as db:
        rows = await db.execute_fetchall(sql, tuple(params))
    return [int(r["media_id"]) for r in rows]


async def _count_photos_of_people(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    people = _people_from_plan(plan, state)
    if not people:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 100)

    total = await _count_photos_for_people(people, plan.min_other_people)
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

    media_ids = await _media_ids_for_people(people, limit, plan.min_other_people)
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
        "reply_text": f"I found {len(results)} matching photo{'s' if len(results) != 1 else ''}. Results are loaded below.",
        "results": results,
        "count": len(results),
        "action": "open_search",
        "action_payload": {"query": plan.query or f"show photos of {' with '.join(people)}"},
        "intent": "SQL_ONLY",
        "explanation": "Deterministic people-set retrieval",
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
            """
            SELECT person_b, shared_photo_count
            FROM v_person_cooccurrence_named
            WHERE person_a LIKE ?
            ORDER BY shared_photo_count DESC
            LIMIT ?
            """,
            (f"%{person}%", limit),
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
            """
            SELECT COUNT(*) AS c
            FROM v_person_cooccurrence_named
            WHERE person_a LIKE ?
              AND person_b NOT LIKE ?
            """,
            (f"%{person}%", f"%{person}%"),
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
        not_like = SQL_AND.join("LOWER(person_name) NOT LIKE ?" for _ in exclude_people)
        exclude_clause = f"AND ({not_like})"
        params.extend([f"%{p.lower()}%" for p in exclude_people])
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
            """
            WITH a AS (
                SELECT person_b AS contact, shared_photo_count AS cnt
                FROM v_person_cooccurrence_named
                WHERE person_a LIKE ?
            ),
            b AS (
                SELECT person_b AS contact, shared_photo_count AS cnt
                FROM v_person_cooccurrence_named
                WHERE person_a LIKE ?
            )
            SELECT a.contact, a.cnt AS with_a, b.cnt AS with_b, (a.cnt + b.cnt) AS score
            FROM a
            JOIN b ON b.contact = a.contact
            WHERE a.contact NOT LIKE ?
              AND a.contact NOT LIKE ?
            ORDER BY score DESC
            LIMIT ?
            """,
            (f"%{person_a}%", f"%{person_b}%", f"%{person_a}%", f"%{person_b}%", limit),
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
            """
            SELECT DISTINCT l.place_label
            FROM v_photos_with_location l
            JOIN v_person_photos p ON p.media_id = l.media_id
            WHERE p.person_name LIKE ?
              AND l.place_label IS NOT NULL
              AND l.place_label != ''
            ORDER BY l.place_label COLLATE NOCASE ASC
            LIMIT 200
            """,
            (f"%{person}%",),
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

    preview = ", ".join(labels[:15])
    suffix = "" if len(labels) <= 15 else f" (+{len(labels)-15} more)"
    payload = {
        "reply_text": f"I found {len(labels)} locations for {person}: {preview}{suffix}",
        "results": [],
        "count": 0,
        "action": "none",
        "action_payload": {},
    }
    new_state = state.model_copy(deep=True)
    new_state.last_people = [person]
    new_state.last_operation = plan.operation
    return ExecutionResult(payload=payload, state=new_state)


async def _last_location(plan: AssistantPlan, state: AssistantState) -> ExecutionResult:
    person = plan.person or (plan.people[0] if plan.people else None) or (state.last_people[0] if state.last_people else None)
    if not person:
        return await _natural_fallback(plan.query or state.last_user_query or "", state, 50)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT l.place_label, l.date_taken, l.media_id
            FROM v_photos_with_location l
            JOIN v_person_photos p ON p.media_id = l.media_id
            WHERE p.person_name LIKE ?
              AND l.place_label IS NOT NULL
              AND l.place_label != ''
            ORDER BY l.date_taken DESC
            LIMIT 1
            """,
            (f"%{person}%",),
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
            """
            SELECT l.place_label, l.date_taken, l.media_id
            FROM v_photos_with_location l
            JOIN v_person_photos p ON p.media_id = l.media_id
            WHERE p.person_name LIKE ?
              AND l.place_label IS NOT NULL
              AND l.place_label != ''
            ORDER BY l.date_taken ASC
            LIMIT 1
            """,
            (f"%{person}%",),
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
            """
            SELECT substr(l.date_taken, 1, 7) AS ym,
                   GROUP_CONCAT(DISTINCT l.place_label) AS places,
                   COUNT(DISTINCT l.media_id) AS photo_count
            FROM v_photos_with_location l
            JOIN v_person_photos p ON p.media_id = l.media_id
            WHERE p.person_name LIKE ?
              AND l.place_label IS NOT NULL
              AND l.place_label != ''
              AND l.date_taken IS NOT NULL
            GROUP BY ym
            ORDER BY ym DESC
            LIMIT ?
            """,
            (f"%{person}%", limit),
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
            """
            SELECT p2.person_name AS companion, COUNT(DISTINCT p2.media_id) AS shared_photo_count
            FROM v_person_photos p1
            JOIN v_person_photos p2 ON p2.media_id = p1.media_id
            JOIN v_photos_with_location l ON l.media_id = p1.media_id
            WHERE p1.person_name LIKE ?
              AND p2.person_name IS NOT NULL
              AND p2.person_name != ''
              AND p2.person_name NOT LIKE ?
              AND l.place_label LIKE ?
              AND substr(l.date_taken, 1, 4) = ?
            GROUP BY p2.person_name
            ORDER BY shared_photo_count DESC, companion COLLATE NOCASE ASC
            LIMIT ?
            """,
            (f"%{person}%", f"%{person}%", f"%{plan.location_term}%", str(plan.year), limit),
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
    return ExecutionResult(payload=payload, state=state)


async def _natural_fallback(query: str, state: AssistantState, limit: int) -> ExecutionResult:
    payload = await natural_search(NaturalSearchRequest(query=query, limit=limit))
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
        "action_payload": {"query": query},
        "intent": payload.get("intent"),
        "explanation": payload.get("explanation"),
        "error": payload.get("error"),
    }
    new_state = state.model_copy(deep=True)
    if results:
        new_state.last_media_ids = [int(r["media_id"]) for r in results]
    return ExecutionResult(payload=out, state=new_state)
