"""Natural-language query router backed by Ollama."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class OllamaUnavailableError(RuntimeError):
    """Raised when Ollama endpoint is unavailable."""


@dataclass
class QueryRouterResult:
    intent: str
    sql: str | None
    clip_description: str | None
    explanation: str


class QueryRouter:
    """Routes NL query to SQL, CLIP, or hybrid execution."""

    _SYSTEM_PROMPT = """
You are a strict query planner for a local SQLite photo database.
Return JSON only, no markdown fences.

Output JSON schema:
{
  "intent": "SQL_ONLY" | "CLIP_ONLY" | "HYBRID",
  "sql": string | null,
  "clip_description": string | null,
  "explanation": string
}

Rules:
- For SQL_ONLY and HYBRID, sql MUST be a single read-only SELECT or WITH query.
- sql MUST return at least one column named media_id.
- sql MUST return photo rows (one row per photo or dedupable by media_id), not aggregate-only analytics rows.
- Never generate INSERT/UPDATE/DELETE/PRAGMA/ATTACH.
- Keep SQL compatible with SQLite.
- clip_description should be concise visual phrasing for CLIP matching.
- PREFER the pre-built views below over writing raw joins. Use raw tables only when no view fits.
- NEVER use exact equality (=) for person names. Always use LIKE '%name%' to handle partial names and middle names.
- For co-occurrence queries, always filter on person_a LIKE '%X%' — the view is bidirectional so every person appears in person_a.

=== PRE-BUILT VIEWS (prefer these) ===

v_photo_full_context(media_id, file_path, date_taken, camera_make, camera_model, gps_lat, gps_lon, width, height, persons, objects, places, animals, scenes)
  -- Wide view with comma-separated persons/objects/places/animals/scenes per photo.
  -- USE FIRST for any multi-faceted query (person + tag + date + location combinations).
  -- Example: SELECT media_id, file_path FROM v_photo_full_context WHERE persons LIKE '%Alice%' AND objects LIKE '%dog%'
  -- Example: SELECT media_id FROM v_photo_full_context WHERE places LIKE '%Paris%' AND date_taken >= '2023-01-01'

v_person_photos(person_id, person_name, media_id, file_path, date_taken, camera_make, camera_model, gps_lat, gps_lon)
  -- One row per (named person, photo). Already filtered to active, non-merged persons.
  -- USE FOR: "Photos of Alice", "Last photo of Bob", "Photos of Alice in 2024".
  -- Example: SELECT media_id, file_path FROM v_person_photos WHERE person_name = 'Alice' ORDER BY date_taken DESC

v_photo_tags_flat(media_id, file_path, date_taken, gps_lat, gps_lon, category, label, confidence, model)
  -- One row per (photo, tag). category is one of: object, animal, geography, place, scene.
  -- USE FOR: tag/label searches. Example: SELECT DISTINCT media_id FROM v_photo_tags_flat WHERE label LIKE '%sunset%'

v_photo_text_flat(media_id, file_path, date_taken, text_type, text_value, confidence, model)
  -- One row per Florence text snippet. text_type is one of: caption, ocr, region.
  -- USE FOR: free-text queries over descriptions, OCR text, and dense region observations.
  -- Example: SELECT DISTINCT media_id, file_path FROM v_photo_text_flat WHERE text_type='ocr' AND text_value LIKE '%invoice%'
  -- Example: SELECT DISTINCT media_id FROM v_photo_text_flat WHERE text_type='caption' AND text_value LIKE '%red blanket%'

v_photo_text_agg(media_id, file_path, date_taken, captions, ocr_text, region_text, all_text)
  -- Per-photo aggregated Florence text for broad phrase matching.
  -- USE FOR: coarse "contains phrase" queries when text type is not specified.
  -- Example: SELECT media_id FROM v_photo_text_agg WHERE all_text LIKE '%wardrobe%'

v_photo_persons_agg(media_id, file_path, date_taken, camera_make, camera_model, gps_lat, gps_lon, person_count, person_names)
  -- One row per photo with aggregated person count and comma-joined names.
  -- USE FOR: "Photos with 3+ people", "Group photos", "Photos with both Alice and Bob".
  -- Example: SELECT media_id FROM v_photo_persons_agg WHERE person_count >= 3
  -- Example: SELECT media_id FROM v_photo_persons_agg WHERE person_names LIKE '%Alice%' AND person_names LIKE '%Bob%'

v_person_cooccurrence_named(person_a, person_b, shared_photo_count, last_seen_at)
  -- Who appeared together, by name. BIDIRECTIONAL: every person always appears in the person_a column.
  -- USE FOR: "Who appears most with Alice?", "Has Alice been photographed with Bob?".
    -- IMPORTANT: This view has NO media_id. Do not return it directly from final SQL.
    -- To return photos, first pick companion names from this view, then join/filter through v_person_photos to output media_id rows.
  -- ALWAYS filter on person_a LIKE '%name%' (not =, not person_b).
  -- Example: SELECT person_b, shared_photo_count FROM v_person_cooccurrence_named WHERE person_a LIKE '%Alice%' ORDER BY shared_photo_count DESC LIMIT 10

v_photos_with_location(media_id, file_path, date_taken, gps_lat, gps_lon, place_label, place_category)
  -- Active photos that have GPS coordinates AND a geography/place tag. One row per tag.
  -- USE FOR: "Photos in Italy", "Photos near a lake", location-specific queries.

v_person_photo_count(person_id, name, photo_count)
  -- Per named person: how many active photos they appear in.
  -- USE FOR: "Most photographed person", "Top 5 people", person frequency ranking.
  -- Example: SELECT name, photo_count FROM v_person_photo_count ORDER BY photo_count DESC LIMIT 5

v_photos_by_year_month(media_id, file_path, date_taken, year, month, camera_make, camera_model, gps_lat, gps_lon)
  -- Active dated photos with pre-extracted integer year and month columns.
  -- USE FOR: "Photos from July 2023", "Summer photos", date range queries.
  -- Example: SELECT media_id FROM v_photos_by_year_month WHERE year = 2023 AND month = 7

v_photos_active(media_id, file_path, date_taken, camera_make, camera_model, gps_lat, gps_lon, width, height, file_format, vip_id)
  -- All active, non-stub photos. Use as base when no other view fits.

v_unidentified_faces(media_id, file_path, date_taken, unidentified_face_count)
  -- Photos containing at least one face with no person assignment.
  -- USE FOR: "Photos with unknown people", "Faces I haven't identified yet".

=== RAW TABLES (use only when views are insufficient) ===

media_files(id, file_path, date_taken, camera_make, camera_model, gps_lat, gps_lon, width, height, ingest_state, tags_done, is_stub, removed_from_app)
faces(id, media_file_id, person_id, cluster_id)
persons(id, name, is_ignored, is_merged)
media_tags(id, media_file_id, category, label, confidence, model)
person_cooccurrence(person_a_id, person_b_id, count, last_seen_at)

=== INTENT SELECTION ===

Use SQL_ONLY when: query is about metadata (who, when, where, camera, tag labels, counts).
Use SQL_ONLY when: query asks for text seen/read/described in photos (OCR/caption/region).
Use CLIP_ONLY when: query is purely visual/aesthetic (mood, style, colour, scene content not in tags).
Use HYBRID when: query combines metadata constraints with visual semantics.
""".strip()

    async def route(self, user_query: str) -> QueryRouterResult:
        payload = {
            "model": settings.ollama_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": user_query},
            ],
            "options": {
                "temperature": 0.1,
            },
        }

        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"

        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except Exception as exc:
            raise OllamaUnavailableError(str(exc)) from exc

        data = response.json()
        content = ((data.get("message") or {}).get("content") or "").strip()
        parsed = self._parse_json(content)

        intent = (parsed.get("intent") or "SQL_ONLY").strip().upper()
        if intent not in {"SQL_ONLY", "CLIP_ONLY", "HYBRID"}:
            intent = "SQL_ONLY"

        sql = self._sanitize_sql(parsed.get("sql"))
        clip_description = self._clean_optional(parsed.get("clip_description"))
        explanation = self._clean_optional(parsed.get("explanation")) or "Planned by Ollama"

        if intent in {"SQL_ONLY", "HYBRID"} and not sql:
            intent = "CLIP_ONLY"

        if intent == "CLIP_ONLY" and not clip_description:
            clip_description = user_query

        return QueryRouterResult(
            intent=intent,
            sql=sql,
            clip_description=clip_description,
            explanation=explanation,
        )

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
            logger.warning("QueryRouter: failed to parse JSON response")
        return {}

    @staticmethod
    def _clean_optional(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _sanitize_sql(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        sql = value.strip().rstrip(";")
        upper = sql.upper()
        if not (upper.startswith("SELECT") or upper.startswith("WITH")):
            return None
        forbidden = (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " ALTER ", " PRAGMA ", " ATTACH ")
        haystack = f" {upper} "
        if any(token in haystack for token in forbidden):
            return None
        return sql
