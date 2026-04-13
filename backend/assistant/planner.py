from __future__ import annotations

import json
import logging
import re

import httpx

from backend.assistant.types import AssistantPlan, AssistantState
from backend.config import settings

logger = logging.getLogger(__name__)


class AssistantPlanner:
    _SYSTEM_PROMPT = """
You are a strict planner for VIP assistant.
Return JSON only, no markdown fences.

Output JSON schema:
{
    "operation": "COUNT_INDEXED_PHOTOS" | "COUNT_NAMED_FACES" | "COUNT_NAMED_PEOPLE" | "COUNT_PHOTOS_OF_PEOPLE" | "COUNT_PEOPLE_WITH_PERSON" | "SHOW_PHOTOS_OF_PEOPLE" | "SHOW_PHOTOS_OF_PEOPLE_IN_LOCATION" | "LIST_OTHER_PEOPLE_IN_PHOTOS_OF_PEOPLE" | "LIST_OTHER_PEOPLE_IN_LAST_RESULTS" | "LIST_BEST_FRIENDS" | "LIST_COMMON_CONTACTS" | "LIST_LOCATIONS" | "LAST_LOCATION" | "FIRST_LOCATION" | "TIMELINE_LOCATIONS" | "LIST_PEOPLE_WITH_PERSON_IN_LOCATION_TIME" | "LIST_LOCATIONS_FOR_LAST_RESULTS" | "FOLLOWUP_SHOW_LAST_RESULTS" | "NATURAL_SEARCH",
  "people": string[],
  "person": string | null,
  "person_a": string | null,
  "person_b": string | null,
  "query": string | null,
  "location_term": string | null,
    "year": number | null,
  "min_other_people": number | null,
  "limit": number | null,
  "explanation": string
}

Rules:
- For "show/them/those/show results" follow-ups, use FOLLOWUP_SHOW_LAST_RESULTS.
- For "where were these photos clicked" or location follow-up over current result set, use LIST_LOCATIONS_FOR_LAST_RESULTS.
- "best friends / most photographed with" => LIST_BEST_FRIENDS.
- "common contacts between A and B" => LIST_COMMON_CONTACTS.
- "list locations for X" => LIST_LOCATIONS.
- "last location of X" => LAST_LOCATION.
- "first/earliest location of X" => FIRST_LOCATION.
- "timeline where X has been" => TIMELINE_LOCATIONS.
- "how many people have clicked photos with X" => COUNT_PEOPLE_WITH_PERSON.
- "who were with X in <place> in <year>" => LIST_PEOPLE_WITH_PERSON_IN_LOCATION_TIME.
- "who else is in X photos with Y" => LIST_OTHER_PEOPLE_IN_PHOTOS_OF_PEOPLE.
- "who else do they appear with in those photos" => LIST_OTHER_PEOPLE_IN_LAST_RESULTS.
- "how many photos of X [with Y...]" => COUNT_PHOTOS_OF_PEOPLE.
- "show photos of X [with Y...]" => SHOW_PHOTOS_OF_PEOPLE.
- "show photos of X from/in/near PLACE" => SHOW_PHOTOS_OF_PEOPLE_IN_LOCATION and set location_term.
- For phrases like "at least N other people", set min_other_people=N.
- Use NATURAL_SEARCH only for broad visual semantics where deterministic ops do not fit.
""".strip()

    async def plan(self, message: str, state: AssistantState, limit: int) -> AssistantPlan:
        planned = await self._plan_via_ollama(message, state, limit)
        if planned is not None:
            return planned
        return self._heuristic_plan(message, state, limit)

    async def _plan_via_ollama(self, message: str, state: AssistantState, limit: int) -> AssistantPlan | None:
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
                            "limit": limit,
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
            logger.warning("AssistantPlanner unavailable: %s", exc)
            return None

        content = ((response.json().get("message") or {}).get("content") or "").strip()
        parsed = self._parse_json(content)
        if not parsed:
            return None
        try:
            plan = AssistantPlan.model_validate(parsed)
        except Exception:
            return None
        if not plan.limit:
            plan.limit = limit
        return plan

    def _heuristic_plan(self, message: str, state: AssistantState, limit: int) -> AssistantPlan:
        lowered = message.lower().strip()

        simple = self._simple_count_or_followup_plan(lowered, limit)
        if simple is not None:
            return simple

        location = self._location_plan(message, lowered, limit)
        if location is not None:
            return location

        relationship = self._relationship_plan(message, lowered, state, limit)
        if relationship is not None:
            return relationship

        photos = self._people_photo_plan(message, lowered, state, limit)
        if photos is not None:
            return photos

        return AssistantPlan(operation="NATURAL_SEARCH", query=message, limit=limit, explanation="Fallback natural search")

    @staticmethod
    def _simple_count_or_followup_plan(lowered: str, limit: int) -> AssistantPlan | None:
        if re.fullmatch(r"(show|open|load)(\s+(them|those|results?))?", lowered):
            return AssistantPlan(operation="FOLLOWUP_SHOW_LAST_RESULTS", limit=limit, explanation="Follow-up show")
        if re.search(r"where\s+were\s+(these|those)\s+photos\s+(clicked|taken)", lowered):
            return AssistantPlan(operation="LIST_LOCATIONS_FOR_LAST_RESULTS", limit=limit, explanation="Locations of last result set")
        if re.search(r"who\s+else\s+do\s+they\s+appear\s+with\s+in\s+those\s+photos", lowered):
            return AssistantPlan(operation="LIST_OTHER_PEOPLE_IN_LAST_RESULTS", limit=limit, explanation="Other people in last result set")
        if re.search(r"(how many\s+.*faces.*named)|(named\s+faces)", lowered):
            return AssistantPlan(operation="COUNT_NAMED_FACES", limit=limit, explanation="Named faces count")
        if re.search(r"(how many\s+.*names?\s+set)|(people\s+named)", lowered):
            return AssistantPlan(operation="COUNT_NAMED_PEOPLE", limit=limit, explanation="Named people count")
        if re.search(r"how many\s+photos?(\s+processed|\s+indexed)?", lowered) and " of " not in lowered:
            return AssistantPlan(operation="COUNT_INDEXED_PHOTOS", limit=limit, explanation="Indexed photo count")
        return None

    def _location_plan(self, message: str, lowered: str, limit: int) -> AssistantPlan | None:
        person = self._extract_person_for_location_query(message)
        if person:
            if re.search(r"first|earliest", lowered) and re.search(r"location", lowered):
                return AssistantPlan(operation="FIRST_LOCATION", person=person, limit=limit, explanation="First location")
            if re.search(r"last\s+location", lowered):
                return AssistantPlan(operation="LAST_LOCATION", person=person, limit=limit, explanation="Last location")
            if re.search(r"timeline|history", lowered):
                return AssistantPlan(operation="TIMELINE_LOCATIONS", person=person, limit=limit, explanation="Location timeline")
            if re.search(r"locations?", lowered):
                return AssistantPlan(operation="LIST_LOCATIONS", person=person, limit=limit, explanation="List locations")

        combo = self._extract_person_location_year(message)
        if combo is not None:
            person_name, place, year = combo
            return AssistantPlan(
                operation="LIST_PEOPLE_WITH_PERSON_IN_LOCATION_TIME",
                person=person_name,
                location_term=place,
                year=year,
                limit=min(limit, 50),
                explanation="People with person at location and year",
            )
        return None

    def _relationship_plan(self, message: str, lowered: str, state: AssistantState, limit: int) -> AssistantPlan | None:
        if re.search(r"how\s+many\s+people\s+have\s+clicked\s+photos?\s+with", lowered):
            person = self._extract_primary_person(message, state)
            if person:
                return AssistantPlan(operation="COUNT_PEOPLE_WITH_PERSON", person=person, limit=limit, explanation="Count people with person")

        if re.search(r"best\s+friends?|most\s+photographed\s+with|friends", lowered):
            person = self._extract_primary_person(message, state)
            if person:
                return AssistantPlan(operation="LIST_BEST_FRIENDS", person=person, limit=min(limit, 20), explanation="Best friends")
        if re.search(r"common\s+contacts", lowered):
            people = self._extract_people(message, state)
            if len(people) >= 2:
                return AssistantPlan(
                    operation="LIST_COMMON_CONTACTS",
                    person_a=people[0],
                    person_b=people[1],
                    limit=min(limit, 20),
                    explanation="Common contacts",
                )

        if re.search(r"who\s+else\s+is\s+in\s+.*photos?\s+with", lowered):
            people = self._extract_people(message, state)
            if people:
                return AssistantPlan(
                    operation="LIST_OTHER_PEOPLE_IN_PHOTOS_OF_PEOPLE",
                    people=people,
                    limit=min(limit, 50),
                    explanation="Other people in matched photos",
                )
        return None

    def _people_photo_plan(self, message: str, lowered: str, state: AssistantState, limit: int) -> AssistantPlan | None:
        min_other = self._extract_min_other_people(lowered)

        people = self._extract_people(message, state)

        location_term = self._extract_location_term(message)
        if location_term and (people or state.last_people):
            resolved_people = people or state.last_people
            return AssistantPlan(
                operation="SHOW_PHOTOS_OF_PEOPLE_IN_LOCATION",
                people=resolved_people,
                location_term=location_term,
                limit=limit,
                explanation="Show photos of people in location",
            )

        if re.search(r"show|open|load", lowered) and people:
            return AssistantPlan(
                operation="SHOW_PHOTOS_OF_PEOPLE",
                people=people,
                min_other_people=min_other,
                limit=limit,
                explanation="Show photos of people",
            )

        if min_other is not None and people:
            return AssistantPlan(
                operation="SHOW_PHOTOS_OF_PEOPLE",
                people=people,
                min_other_people=min_other,
                limit=limit,
                explanation="Implicit show with min-people constraint",
            )

        if re.search(r"how many\s+photos?", lowered) and people:
            return AssistantPlan(
                operation="COUNT_PHOTOS_OF_PEOPLE",
                people=people,
                min_other_people=min_other,
                limit=limit,
                explanation="Count photos of people",
            )

        if state.last_people and re.search(r"^\s*(and\s+)?with\s+", lowered):
            extras = self._extract_with_followup_people(message)
            merged = self._merge_people(state.last_people, extras)
            op = "COUNT_PHOTOS_OF_PEOPLE" if "how many" in lowered else "SHOW_PHOTOS_OF_PEOPLE"
            return AssistantPlan(operation=op, people=merged, limit=limit, explanation="With-followup merge")
        return None

    @staticmethod
    def _extract_location_term(message: str) -> str | None:
        patterns = [
            r"\bfrom\s+([^?.!,]+)",
            r"\bin\s+([^?.!,]+)",
            r"\bnear\s+([^?.!,]+)",
            r"\bby\s+the\s+([^?.!,]+)",
        ]
        for pat in patterns:
            m = re.search(pat, message, flags=re.IGNORECASE)
            if not m:
                continue
            raw = m.group(1).strip().strip("'\"")
            # Trim common trailing tokens that indicate a new clause.
            raw = re.split(r"\s+(?:with|and|where|that|which)\b", raw, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            if raw:
                return raw
        return None

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
            logger.warning("AssistantPlanner failed to parse JSON")
        return {}

    @staticmethod
    def _extract_people(message: str, state: AssistantState) -> list[str]:
        # Check for "his/her/their photos from/of [location/name]"
        m_pronoun = re.search(r"\b(?:his|her|their)\s+photos?\b", message, flags=re.IGNORECASE)
        if m_pronoun and state.last_people:
            # Extract person from context when pronoun is used
            return [state.last_people[0]]

        m = re.search(r"photos?\s+of\s+(.+)$", message, flags=re.IGNORECASE)
        if m:
            tail = m.group(1).strip().rstrip("?.!")
            parts = re.split(r"\s+with\s+|\s+and\s+|\s*,\s*", tail, flags=re.IGNORECASE)
            return [p.strip().strip("'\"") for p in parts if p.strip()]

        m_pos = re.search(r"([A-Za-z][A-Za-z\s\-']{1,40})'s\s+photos?", message)
        if m_pos:
            person = m_pos.group(1).strip()
            return [person] if person else []

        m2 = re.search(r"^\s*(and\s+)?with\s+(.+)$", message, flags=re.IGNORECASE)
        if m2 and state.last_people:
            extras = [p.strip() for p in re.split(r"\s+and\s+|\s*,\s*", m2.group(2).strip()) if p.strip()]
            return AssistantPlanner._merge_people(state.last_people, extras)

        return []

    @staticmethod
    def _extract_with_followup_people(message: str) -> list[str]:
        m = re.search(r"^\s*(and\s+)?with\s+(.+)$", message, flags=re.IGNORECASE)
        if not m:
            return []
        return [p.strip() for p in re.split(r"\s+and\s+|\s*,\s*", m.group(2).strip()) if p.strip()]

    @staticmethod
    def _merge_people(base: list[str], extras: list[str]) -> list[str]:
        out = list(base)
        seen = {p.lower() for p in out}
        for p in extras:
            if p.lower() not in seen:
                out.append(p)
                seen.add(p.lower())
        return out

    @staticmethod
    def _extract_min_other_people(lowered: str) -> int | None:
        m = re.search(r"at least\s+(\d+)\s+other\s+people", lowered)
        if not m:
            return None
        return max(0, min(int(m.group(1)), 100))

    @staticmethod
    def _extract_primary_person(message: str, state: AssistantState) -> str | None:
        people = AssistantPlanner._extract_people(message, state)
        if people:
            return people[0]
        m = re.search(r"([A-Za-z][A-Za-z\s\-']{1,40})'s", message)
        if m:
            return m.group(1).strip()
        if state.last_people:
            return state.last_people[0]
        return None

    @staticmethod
    def _extract_person_for_location_query(text: str) -> str | None:
        patterns = [
            r"locations?\s+for\s+(.+)$",
            r"where\s+has\s+(.+?)\s+been",
            r"last\s+location\s+for\s+(.+)$",
            r"last\s+location\s+of\s+(.+)$",
            r"what\s+was\s+(.+?)'s\s+last\s+location",
        ]
        for pat in patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                person = m.group(1).strip().rstrip("?.!")
                if person:
                    return person
        return None

    @staticmethod
    def _extract_person_location_year(text: str) -> tuple[str, str, int] | None:
        m = re.search(
            r"with\s+([A-Z][A-Z ]{1,40})\s+in\s+(.+?)\s+in\s+(\d{4})",
            text,
            flags=re.IGNORECASE,
        )
        if not m:
            return None
        person = m.group(1).strip().rstrip("?.!")
        place = m.group(2).strip().rstrip("?.!")
        year = int(m.group(3))
        return person, place, year
