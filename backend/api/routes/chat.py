"""VIP API — Assistant chat route (Phase 2)."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter
from pydantic import BaseModel

from backend.assistant.executor import continue_pending_person_clarification, execute_plan
from backend.assistant.memory import append_turn, get_or_create_session, save_state
from backend.assistant.planner import AssistantPlanner
from backend.assistant.types import AssistantPlan

router = APIRouter()
_planner = AssistantPlanner()


def _normalize_plan_for_message(plan: AssistantPlan, message: str) -> AssistantPlan:
    lowered = message.lower()
    asks_count = bool(re.search(r"\b(how many|count|number of)\b", lowered))
    asks_show = bool(re.search(r"\b(show|open|load)\b", lowered))

    if plan.operation == "COUNT_PHOTOS_OF_PEOPLE" and not asks_count and (asks_show or "photo" in lowered):
        plan.operation = "SHOW_PHOTOS_OF_PEOPLE"

    if plan.operation == "SHOW_PHOTOS_OF_PEOPLE" and asks_count and not asks_show:
        plan.operation = "COUNT_PHOTOS_OF_PEOPLE"

    return plan


class ChatRequest(BaseModel):
    message: str
    limit: int = 50
    offset: int = 0
    conversation_id: str | None = None


@router.post("/message")
async def chat_message(req: ChatRequest):
    message = (req.message or "").strip()
    limit = max(1, min(int(req.limit or 50), 200))
    offset = max(0, int(req.offset or 0))

    session_id, state = await get_or_create_session(req.conversation_id)

    if not message:
        payload = {
            "conversation_id": session_id,
            "reply_text": "Ask me anything about your photos.",
            "results": [],
            "count": 0,
            "action": "none",
            "action_payload": {},
        }
        await append_turn(session_id, role="assistant", message=payload["reply_text"], response_json=json.dumps(payload))
        return payload

    await append_turn(session_id, role="user", message=message)

    # Keep state updated with last user query before planning.
    state.last_user_query = message

    clarification = await continue_pending_person_clarification(message, state, limit)
    if clarification is not None:
        executed, plan = clarification
    else:
        plan = await _planner.plan(message, state, limit)
        if not isinstance(plan, AssistantPlan):
            plan = AssistantPlan(operation="NATURAL_SEARCH", query=message, limit=limit, explanation="Planner fallback")
        plan = _normalize_plan_for_message(plan, message)
        plan.offset = offset
        executed = await execute_plan(plan, state, limit)

    next_state = executed.state
    next_state.last_user_query = message
    if executed.payload.get("action") != "needs_clarification":
        next_state.last_operation = plan.operation
        if plan.people:
            next_state.last_people = [p for p in plan.people if p]

    await save_state(session_id, next_state)

    payload = dict(executed.payload)
    payload["conversation_id"] = session_id
    if "action_payload" not in payload:
        payload["action_payload"] = {}

    await append_turn(
        session_id,
        role="assistant",
        message=payload.get("reply_text", ""),
        plan_json=plan.model_dump_json(),
        response_json=json.dumps(payload),
    )

    return payload
