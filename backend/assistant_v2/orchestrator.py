from __future__ import annotations

import re
import time
from typing import Any

from backend.assistant.types import AssistantState
from backend.assistant_v2.planner import AssistantV2ToolPlanner
from backend.assistant_v2.tools import ToolContext, ToolRegistry, build_default_registry
from backend.assistant_v2.types import AssistantV2Response, ToolCallTrace


_TRIM_CHARS = "?.!,\"'"
_NAME_CAPTURE = r"([A-Za-z][A-Za-z '-]{1,50})"


class AssistantV2Orchestrator:
    _MAX_TOOL_STEPS = 2
    _MESSAGE_AWARE_TOOLS = {
        "show_photos_of_people",
        "count_photos_of_people",
        "natural_search",
        "retrieval_broker",
        "sql_agent",
        "legacy_assistant",
        "list_people_with_person_in_location",
    }

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()
        self.planner = AssistantV2ToolPlanner()

    async def handle(
        self,
        message: str,
        session_id: str,
        state: AssistantState,
        limit: int,
        offset: int,
    ) -> tuple[AssistantV2Response, AssistantState]:
        traces: list[ToolCallTrace] = []
        current_state = state

        tool_name, params, planner_notes = await self._pick_initial_tool(message, current_state)
        ctx = ToolContext(session_id=session_id, state=current_state, limit=limit, offset=offset)

        final_payload: dict[str, Any] = {}
        used_tools: set[str] = set()

        for step_idx in range(self._MAX_TOOL_STEPS):
            started = time.perf_counter()
            result = await self.registry.execute(tool_name, ctx, params)
            latency_ms = int((time.perf_counter() - started) * 1000)

            trace = ToolCallTrace(
                tool_name=tool_name,
                status="ok" if result.payload.get("error") is None else "error",
                latency_ms=latency_ms,
                notes=f"step:{step_idx + 1};{planner_notes};{result.notes or ''}".rstrip(";"),
                input=params,
            )
            traces.append(trace)
            used_tools.add(tool_name)

            final_payload = result.payload
            current_state = result.next_state

            followup = self._choose_followup_tool(message, final_payload, used_tools, tool_name)
            if followup is None:
                break

            tool_name, params, planner_notes = followup
            ctx = ToolContext(session_id=session_id, state=current_state, limit=limit, offset=offset)

        response = self._to_v2_response(session_id, final_payload, traces)
        return response, current_state

    async def _pick_initial_tool(
        self,
        message: str,
        state: AssistantState,
    ) -> tuple[str, dict[str, Any], str]:
        selection = await self.planner.select_tool(message, state, self.registry)
        if selection is not None:
            params = self._ensure_message_param(selection.tool_name, selection.params, message)
            return (
                selection.tool_name,
                params,
                f"planner:{selection.source}:{selection.reason}",
            )

        tool_name, params = self._choose_tool(message, state)
        return tool_name, self._ensure_message_param(tool_name, params, message), "planner:heuristic"

    def _ensure_message_param(self, tool_name: str, params: dict[str, Any], message: str) -> dict[str, Any]:
        if tool_name not in self._MESSAGE_AWARE_TOOLS:
            return params
        next_params = dict(params)
        if not str(next_params.get("message") or "").strip():
            next_params["message"] = message
        return next_params

    def _choose_followup_tool(
        self,
        message: str,
        payload: dict[str, Any],
        used_tools: set[str],
        last_tool_name: str,
    ) -> tuple[str, dict[str, Any], str] | None:
        if payload.get("error"):
            return None

        action = str(payload.get("action") or "none")
        lowered = (message or "").lower()
        if action == "needs_clarification":
            return self._clarification_followup(lowered, message, used_tools, last_tool_name)

        if self._should_fallback_open_search_zero(action, payload, used_tools, last_tool_name):
            return "legacy_assistant", {"message": message}, "fallback:open_search_zero_to_legacy"

        if action == "open_search":
            return None

        if self._has_resolved_data(payload):
            return None

        wants_photos = bool(re.search(r"\b(show|open|load|find|search|photos?|images?)\b", lowered))
        if wants_photos and "retrieval_broker" not in used_tools:
            return "retrieval_broker", {"message": message}, "fallback:no_results_to_retrieval_broker"
        if wants_photos and "natural_search" not in used_tools:
            return "natural_search", {"query": message}, "fallback:no_results_to_natural_search"

        # If deterministic relational/location tools yield no data, try legacy bridge once.
        no_data_tools = {
            "list_best_friends",
            "list_locations",
            "list_common_contacts",
            "count_photos_of_people",
            "show_photos_of_people",
        }
        if last_tool_name in no_data_tools and "legacy_assistant" not in used_tools:
            return "legacy_assistant", {"message": message}, "fallback:no_data_to_legacy"

        return None

    def _clarification_followup(
        self,
        lowered: str,
        message: str,
        used_tools: set[str],
        last_tool_name: str,
    ) -> tuple[str, dict[str, Any], str] | None:
        if (
            last_tool_name == "count_photos_of_people"
            and self._match_sql_agent_tool(lowered)
            and "sql_agent" not in used_tools
        ):
            return "sql_agent", {"message": message}, "fallback:clarification_to_sql_agent"
        if (
            last_tool_name == "sql_agent"
            and self._match_person_location_copresence_tool(lowered)
            and "list_people_with_person_in_location" not in used_tools
        ):
            return "list_people_with_person_in_location", {"message": message}, "fallback:sql_agent_to_location_copresence"
        return None

    @staticmethod
    def _has_resolved_data(payload: dict[str, Any]) -> bool:
        return bool(payload.get("results") or payload.get("face_results") or int(payload.get("count") or 0) > 0)

    @staticmethod
    def _should_fallback_open_search_zero(
        action: str,
        payload: dict[str, Any],
        used_tools: set[str],
        last_tool_name: str,
    ) -> bool:
        return bool(
            action == "open_search"
            and not payload.get("results")
            and int(payload.get("count") or 0) == 0
            and last_tool_name == "show_photos_of_people"
            and "legacy_assistant" not in used_tools
        )

    def _choose_tool(self, message: str, state: AssistantState) -> tuple[str, dict[str, Any]]:
        lowered = (message or "").lower().strip()
        if not lowered:
            return "natural_search", {"query": ""}

        simple = self._match_simple_count_tools(lowered)
        if simple is not None:
            return simple

        face = self._match_face_scope_tool(lowered)
        if face is not None:
            return face

        relation = self._match_relation_tools(lowered, message, state)
        if relation is not None:
            return relation

        people_photo = self._match_people_photo_tools(lowered, message)
        if people_photo is not None:
            return people_photo

        location = self._match_location_tool(lowered, message, state)
        if location is not None:
            return location

        if self._match_sql_agent_tool(lowered):
            return "sql_agent", {"message": message}

        if re.search(r"\b(find|search|show|open|photos?|images?)\b", lowered):
            return "retrieval_broker", {"message": message}

        # Compatibility-first default while V2 tool coverage grows.
        return "legacy_assistant", {"message": message}

    @staticmethod
    def _match_simple_count_tools(lowered: str) -> tuple[str, dict[str, Any]] | None:
        for pattern, tool_name in (
            (r"(named\s+faces|faces\s+named|identified\s+faces)", "count_named_faces"),
            (r"(named\s+people|people\s+named|names?\s+set)", "count_named_people"),
            (r"how many\s+photos?(\s+are)?\s+(processed|indexed)", "count_indexed_photos"),
        ):
            if re.search(pattern, lowered):
                return tool_name, {}
        return None

    @staticmethod
    def _match_face_scope_tool(lowered: str) -> tuple[str, dict[str, Any]] | None:
        if re.search(r"(unnamed|unidentified|unknown)\s+faces?", lowered):
            scoped = bool(re.search(r"(these|those|current)\s+(photos|results)", lowered))
            return "show_unnamed_faces", {"use_last_results": scoped}
        return None

    def _match_relation_tools(
        self,
        lowered: str,
        message: str,
        state: AssistantState,
    ) -> tuple[str, dict[str, Any]] | None:
        if self._match_person_location_copresence_tool(lowered):
            return "list_people_with_person_in_location", {"message": message}

        if re.search(r"who\s+(?:is|are|else\s+is|else\s+are).*\bwith\b", lowered):
            if re.search(r"\b(him|her|them|these\s+photos|those\s+photos|in\s+these\s+photos|in\s+those\s+photos)\b", lowered):
                return "list_other_people_in_last_results", {}

        if re.search(r"(best\s+friends?|most\s+photographed\s+with|friends)", lowered):
            person = self._extract_person(message, state)
            return "list_best_friends", {"person": person or ""}

        if re.search(r"common\s+contacts", lowered):
            pair = self._extract_person_pair(message)
            if pair is not None:
                return "list_common_contacts", {"person_a": pair[0], "person_b": pair[1]}
            return "list_common_contacts", {"person_a": "", "person_b": ""}
        return None

    @staticmethod
    def _match_people_photo_tools(lowered: str, message: str) -> tuple[str, dict[str, Any]] | None:
        for pattern, tool_name in (
            (r"how\s+many\s+photos?\s+of", "count_photos_of_people"),
            (r"(show|open|load)\s+photos?\s+of", "show_photos_of_people"),
        ):
            if re.search(pattern, lowered):
                return tool_name, {"message": message}
        return None

    def _match_location_tool(
        self,
        lowered: str,
        message: str,
        state: AssistantState,
    ) -> tuple[str, dict[str, Any]] | None:
        if re.search(r"(list|show|what\s+are).*(locations?)", lowered) or re.search(r"where\s+is\s+.*(typically\s+found|usually\s+found|found|seen|been)", lowered):
            person = self._extract_person(message, state)
            return "list_locations", {"person": person or ""}
        return None

    @staticmethod
    def _match_sql_agent_tool(lowered: str) -> bool:
        # Broad analytical/chat prompts that are not explicit photo-open commands.
        return bool(
            re.search(r"\b(top|most|least|average|avg|distribution|breakdown|summary|trend|across|compare|which|what|who)\b", lowered)
            and not re.search(r"\b(show|open|load)\s+photos?\b", lowered)
        )

    @staticmethod
    def _match_person_location_copresence_tool(lowered: str) -> bool:
        if not re.search(r"\bwho\b.*\bwith\b", lowered):
            return False
        if not re.search(r"\b(in|from|near|at)\b", lowered):
            return False
        if re.search(r"\b(these\s+photos|those\s+photos|in\s+these\s+photos|in\s+those\s+photos)\b", lowered):
            return False
        return True

    @staticmethod
    def _extract_person(message: str, state: AssistantState) -> str | None:
        text = (message or "").strip()

        direct_patterns = [
            rf"who\s+is\s+{_NAME_CAPTURE}\s+most\s+photographed\s+with",
            rf"best\s+friends?\s+of\s+{_NAME_CAPTURE}",
            rf"where\s+is\s+{_NAME_CAPTURE}\s+(?:typically\s+found|usually\s+found|found|seen|been)",
        ]
        for pattern in direct_patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if not m:
                continue
            candidate = m.group(1).strip().strip(_TRIM_CHARS)
            if candidate:
                return candidate

        patterns = [
            rf"with\s+{_NAME_CAPTURE}",
            rf"for\s+{_NAME_CAPTURE}",
            rf"of\s+{_NAME_CAPTURE}",
            rf"{_NAME_CAPTURE}'s\s+(?:photos?|locations?)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if not m:
                continue
            candidate = m.group(1).strip().strip(_TRIM_CHARS)
            candidate = re.split(r"\s+(?:in|from|near|at|and|with)\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            if candidate:
                return candidate

        if state.last_people:
            return state.last_people[0]
        return None

    @staticmethod
    def _extract_person_pair(message: str) -> tuple[str, str] | None:
        text = (message or "").strip()
        m = re.search(rf"between\s+{_NAME_CAPTURE}\s+and\s+{_NAME_CAPTURE}", text, flags=re.IGNORECASE)
        if not m:
            return None
        a = m.group(1).strip().strip(_TRIM_CHARS)
        b = m.group(2).strip().strip(_TRIM_CHARS)
        if not a or not b:
            return None
        return a, b

    @staticmethod
    def _to_v2_response(
        conversation_id: str,
        payload: dict[str, Any],
        traces: list[ToolCallTrace],
    ) -> AssistantV2Response:
        action = str(payload.get("action") or "none")
        if action == "needs_clarification":
            action_type = "clarification"
        elif action == "open_search":
            action_type = "open_search"
        elif payload.get("error"):
            action_type = "tool_error"
        else:
            action_type = "none"

        return AssistantV2Response(
            conversation_id=conversation_id,
            reply_text=str(payload.get("reply_text") or ""),
            action_type=action_type,
            action_payload=payload.get("action_payload") or {},
            results=payload.get("results") or [],
            face_results=payload.get("face_results") or [],
            count=int(payload.get("count") or 0),
            intent=payload.get("intent"),
            explanation=payload.get("explanation"),
            tool_trace=traces,
        )
