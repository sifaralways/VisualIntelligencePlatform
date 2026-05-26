from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from backend.assistant.types import AssistantState
from backend.assistant_v2.tools import ToolRegistry
from backend.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ToolSelection:
    tool_name: str
    params: dict[str, Any]
    reason: str
    source: str


class AssistantV2ToolPlanner:
    _SYSTEM_PROMPT = """
You are a strict planner for VIP AssistantV2.
Pick exactly one tool and parameters.
Return JSON only, no markdown.

Output schema:
{
  "tool_name": string,
  "params": object,
  "reason": string
}

Rules:
- tool_name must be one of the provided tools.
- params must match the tool's input schema as closely as possible.
- If required params are missing from user query, still pick the best tool and leave missing fields empty.
- Prefer deterministic tools over legacy fallback when a deterministic tool clearly matches.
- Use natural_search for open-ended visual queries.
- Use sql_agent for open-ended analytical/database questions (top/most/least/breakdown/compare/trend/count by group) that are not explicit direct photo-opening commands.
- For prompts like "top people by photo count" or "top locations where X appears", prefer sql_agent unless a dedicated deterministic tool exactly fits.
- For prompts like "who was with <person/pronoun> in <location> [in <year>]", prefer list_people_with_person_in_location.
- For prompts like "where is <person> typically found" or "where has <person> been", prefer list_locations.
- Use legacy_assistant only when none of the deterministic tools fit.
""".strip()

    async def select_tool(
        self,
        message: str,
        state: AssistantState,
        registry: ToolRegistry,
    ) -> ToolSelection | None:
        tools = [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
            for spec in registry.list_tools()
        ]

        payload = {
            "model": settings.ollama_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message,
                            "state": state.model_dump(),
                            "available_tools": tools,
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
            logger.warning("AssistantV2ToolPlanner unavailable: %s", exc)
            return None

        content = ((response.json().get("message") or {}).get("content") or "").strip()
        parsed = self._parse_json(content)
        if not parsed:
            return None

        tool_name = str(parsed.get("tool_name") or "").strip()
        if not tool_name:
            return None
        if registry.get(tool_name) is None:
            return None

        params = parsed.get("params")
        if not isinstance(params, dict):
            params = {}

        reason = str(parsed.get("reason") or "planned by llm").strip()
        return ToolSelection(
            tool_name=tool_name,
            params=params,
            reason=reason,
            source="llm",
        )

    def _parse_json(self, content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            logger.warning("AssistantV2ToolPlanner failed to parse JSON")
        return {}
