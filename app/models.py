from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    ok: bool
    service: str


class AgentResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    models: list["ModelResponse"] = []
    default_model: str | None = None
    requires_model: bool = False


class ModelResponse(BaseModel):
    id: str
    name: str
    description: str = ""


class CreateSessionRequest(BaseModel):
    agent_id: str | None = None
    model_id: str | None = None
    client: str | None = None
    title: str | None = None
    workspace_path: str | None = None


class SessionResponse(BaseModel):
    session_id: str
    agent_id: str
    model_id: str | None = None
    title: str
    client: str
    workspace_path: str | None = None
    status: str
    created_at: str
    updated_at: str
    message_count: int = 0
    last_message_preview: str = ""
    has_native_binding: bool = False


class SessionsResponse(BaseModel):
    sessions: list[SessionResponse]


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1)


class EventResponse(BaseModel):
    event_id: str
    session_id: str
    role: str
    content: str
    created_at: str


class HistoryResponse(BaseModel):
    session_id: str
    messages: list[EventResponse]
