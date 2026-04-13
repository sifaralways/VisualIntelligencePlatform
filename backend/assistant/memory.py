from __future__ import annotations

import json
import uuid

from backend.database.db import get_db
from backend.assistant.types import AssistantState


async def get_or_create_session(session_id: str | None) -> tuple[str, AssistantState]:
    sid = (session_id or "").strip() or str(uuid.uuid4())
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT state_json FROM chat_sessions WHERE id = ?",
            (sid,),
        )
        if rows:
            state_raw = rows[0]["state_json"] or "{}"
            try:
                payload = json.loads(state_raw)
            except Exception:
                payload = {}
            return sid, AssistantState.model_validate(payload)

        await db.execute(
            "INSERT INTO chat_sessions(id, state_json) VALUES(?, ?)",
            (sid, "{}"),
        )
    return sid, AssistantState()


async def save_state(session_id: str, state: AssistantState) -> None:
    async with get_db() as db:
        await db.execute(
            """
            UPDATE chat_sessions
            SET state_json = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (state.model_dump_json(), session_id),
        )


async def append_turn(
    session_id: str,
    role: str,
    message: str,
    plan_json: str | None = None,
    response_json: str | None = None,
) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO chat_turns(session_id, role, message, plan_json, response_json)
            VALUES(?, ?, ?, ?, ?)
            """,
            (session_id, role, message, plan_json, response_json),
        )
