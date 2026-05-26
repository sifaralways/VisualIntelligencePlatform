from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AssistantV2Request(BaseModel):
    message: str
    limit: int = 50
    offset: int = 0
    conversation_id: str | None = None


class ToolCallTrace(BaseModel):
    tool_name: str
    status: Literal["ok", "error"] = "ok"
    latency_ms: int | None = None
    notes: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class AssistantV2Response(BaseModel):
    version: Literal["v2"] = "v2"
    conversation_id: str
    reply_text: str
    action_type: Literal["none", "open_search", "clarification", "tool_error"] = "none"
    action_payload: dict[str, Any] = Field(default_factory=dict)
    results: list[dict[str, Any]] = Field(default_factory=list)
    face_results: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0
    intent: str | None = None
    explanation: str | None = None
    tool_trace: list[ToolCallTrace] = Field(default_factory=list)
