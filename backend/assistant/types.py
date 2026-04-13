from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


Operation = Literal[
    "COUNT_INDEXED_PHOTOS",
    "COUNT_NAMED_FACES",
    "COUNT_NAMED_PEOPLE",
    "COUNT_PHOTOS_OF_PEOPLE",
    "COUNT_PEOPLE_WITH_PERSON",
    "SHOW_PHOTOS_OF_PEOPLE",
    "LIST_OTHER_PEOPLE_IN_PHOTOS_OF_PEOPLE",
    "LIST_OTHER_PEOPLE_IN_LAST_RESULTS",
    "LIST_BEST_FRIENDS",
    "LIST_COMMON_CONTACTS",
    "LIST_LOCATIONS",
    "LAST_LOCATION",
    "FIRST_LOCATION",
    "TIMELINE_LOCATIONS",
    "LIST_PEOPLE_WITH_PERSON_IN_LOCATION_TIME",
    "LIST_LOCATIONS_FOR_LAST_RESULTS",
    "FOLLOWUP_SHOW_LAST_RESULTS",
    "NATURAL_SEARCH",
]


class AssistantPlan(BaseModel):
    operation: Operation = "NATURAL_SEARCH"
    people: list[str] = Field(default_factory=list)
    person: str | None = None
    person_a: str | None = None
    person_b: str | None = None
    query: str | None = None
    location_term: str | None = None
    year: int | None = None
    min_other_people: int | None = None
    limit: int | None = None
    explanation: str = ""


class AssistantState(BaseModel):
    last_user_query: str | None = None
    last_operation: str | None = None
    last_people: list[str] = Field(default_factory=list)
    last_media_ids: list[int] = Field(default_factory=list)
