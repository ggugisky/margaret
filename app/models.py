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
    session_key: str | None = None
    agent_id: str
    model_id: str | None = None
    title: str
    client: str
    source_label: str | None = None
    workspace_path: str | None = None
    status: str
    created_at: str
    updated_at: str
    message_count: int = 0
    last_message_preview: str = ""
    has_native_binding: bool = False


class SessionsResponse(BaseModel):
    sessions: list[SessionResponse]


class LocationPayload(BaseModel):
    lat: float | None = None
    lng: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy: float | None = None
    altitude: float | None = None
    heading: float | None = None
    speed: float | None = None
    timestamp: int | float | str | None = None


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1)
    location: LocationPayload | None = None


class RoutePointPayload(BaseModel):
    lat: float
    lng: float
    ts: int | float | str | None = None
    speed: float | None = None
    mode: str | None = None
    photo_url: str | None = None


class SaveRouteRequest(BaseModel):
    title: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    duration_sec: int | float | None = None
    distance_m: int | float | None = None
    step_count: int | None = None
    modes: list[str] = []
    start_lat: float | None = None
    start_lng: float | None = None
    end_lat: float | None = None
    end_lng: float | None = None
    points: list[RoutePointPayload] = []


class RouteResponse(BaseModel):
    route_id: str
    title: str
    start_time: str | None = None
    end_time: str | None = None
    duration_sec: float | None = None
    distance_m: float | None = None
    step_count: int | None = None
    modes: list[str] = []
    start_lat: float | None = None
    start_lng: float | None = None
    end_lat: float | None = None
    end_lng: float | None = None
    points: list[dict] = []
    created_at: str


class RoutesResponse(BaseModel):
    routes: list[RouteResponse]


class AuthRequest(BaseModel):
    secret: str


class EventResponse(BaseModel):
    event_id: str
    session_id: str
    role: str
    content: str
    created_at: str


class HistoryResponse(BaseModel):
    session_id: str
    messages: list[EventResponse]
