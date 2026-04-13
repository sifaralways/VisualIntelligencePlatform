"""Assistant chat planner backed by Ollama.

Produces a strict tool-call style plan in JSON so the backend can execute
deterministic handlers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ChatPlan:
    tool: str
    people: list[str]
    query: str | None
    limit: int | None
    explanation: str


class ChatPlanner:
    _SYSTEM_PROMPT = """
You are a strict planner for VIP assistant.
Return JSON only, no markdown fences.

Output JSON schema:
{
  "tool": "COUNT_INDEXED_PHOTOS" | "COUNT_NAMED_FACES" | "COUNT_NAMED_PEOPLE" | "COUNT_PHOTOS_OF_PEOPLE" | "SHOW_PHOTOS_OF_PEOPLE" | "NATURAL_SEARCH",
  "people": string[],
  "query": string | null,
  "limit": number | null,
  "explanation": string
}

Rules:
- Use COUNT_INDEXED_PHOTOS for prompts like "how many photos processed/indexed".
- Use COUNT_NAMED_FACES for prompts about named faces/identified faces.
- Use COUNT_NAMED_PEOPLE for prompts like "how many names set" / "how many people named".
- Use COUNT_PHOTOS_OF_PEOPLE for "how many photos of X" and "how many photos of X with Y".
- Use SHOW_PHOTOS_OF_PEOPLE for "show photos of X" and "show photos of X with Y".
- Use NATURAL_SEARCH for visual/spatial semantics (ocean, beach, mountain, sunset, style, mood) or anything ambiguous.
- people must contain extracted person name fragments when relevant.
- query must echo the user request for NATURAL_SEARCH; else null.
- Keep explanation concise.
""".strip()

    async def plan(self, user_query: str, limit: int = 100) -> ChatPlan | None:
        payload = {
            "model": settings.ollama_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": user_query},
            ],
            "options": {"temperature": 0.0},
        }

        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except Exception as exc:
            logger.warning("ChatPlanner unavailable: %s", exc)
            return None

        content = ((response.json().get("message") or {}).get("content") or "").strip()
        parsed = self._parse_json(content)
        if not parsed:
            return None

        tool = self._normalize_tool(parsed.get("tool"))
        people = self._normalize_people(parsed.get("people"))
        query = self._normalize_query(parsed.get("query"))
        plan_limit = self._normalize_limit(parsed.get("limit"), limit)
        explanation = str(parsed.get("explanation") or "Planned by assistant").strip()
        return ChatPlan(
            tool=tool,
            people=people,
            query=query,
            limit=plan_limit,
            explanation=explanation,
        )

    @staticmethod
    def _normalize_tool(value: object) -> str:
        tool = str(value or "NATURAL_SEARCH").strip().upper()
        allowed = {
            "COUNT_INDEXED_PHOTOS",
            "COUNT_NAMED_FACES",
            "COUNT_NAMED_PEOPLE",
            "COUNT_PHOTOS_OF_PEOPLE",
            "SHOW_PHOTOS_OF_PEOPLE",
            "NATURAL_SEARCH",
        }
        return tool if tool in allowed else "NATURAL_SEARCH"

    @staticmethod
    def _normalize_people(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            s = str(item).strip()
            if s:
                out.append(s)
        return out

    @staticmethod
    def _normalize_query(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text or None

    @staticmethod
    def _normalize_limit(value: object, default_limit: int) -> int:
        if isinstance(value, int):
            return max(1, min(value, 200))
        if isinstance(value, float):
            return max(1, min(int(value), 200))
        return max(1, min(int(default_limit), 200))

    def _parse_json(self, content: str) -> dict:
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
            logger.warning("ChatPlanner failed to parse JSON")
        return {}
