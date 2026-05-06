from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

from fastapi import (  # pyright: ignore[reportMissingImports]
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
)
from fastapi.responses import StreamingResponse  # pyright: ignore[reportMissingImports]

from app.adapters import AdapterState, registry
from app.config import settings
from app.location_context import text_with_location_context
from app.models import (
    AgentResponse,
    AuthRequest,
    CreateSessionRequest,
    EventResponse,
    HealthResponse,
    HistoryResponse,
    SessionsResponse,
    ModelResponse,
    RouteResponse,
    RoutesResponse,
    SaveRouteRequest,
    SendMessageRequest,
    SessionResponse,
)
from app.security import create_signed_token, verify_signed_token
from app.store import Store, utc_now
from app.slack import SlackDMHandler, SlackIntegration
from app.voice import PhoneVoiceWebSocketHandler, VoiceService


app = FastAPI(title="Margaret Gateway", version="0.1.0")
store = Store(settings.database_path)
slack_integration = SlackIntegration(
    settings=settings,
    handler=SlackDMHandler(
        store=store,
        registry=registry,
        default_agent=settings.default_agent,
        workspace_root="/workspace",
    ),
)

_session_locks: dict[str, asyncio.Lock] = {}


def _get_session_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


@app.on_event("startup")
async def startup_event() -> None:
    store.recover_stale_sessions()
    await slack_integration.connect()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await slack_integration.close()


def require_auth(authorization: str | None = Header(default=None)) -> None:
    if not (settings.gateway_token or settings.voice_jwt_secret):
        return
    token = (authorization or "").removeprefix("Bearer ").removeprefix("bearer ").strip()
    if settings.gateway_token and token == settings.gateway_token:
        return
    if settings.voice_jwt_secret and verify_signed_token(
        token, secret=settings.voice_jwt_secret
    ):
        return
    if settings.voice_app_secret and token == settings.voice_app_secret:
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _auth_token_from_ws(websocket: WebSocket) -> str:
    auth_header = (
        websocket.headers.get("authorization")
        or websocket.headers.get("Authorization")
        or ""
    )
    token = auth_header.removeprefix("Bearer ").removeprefix("bearer ").strip()
    return token or websocket.query_params.get("token", "").strip()


def _agent_models_payload() -> list[dict[str, str]]:
    models: list[dict[str, str]] = []
    for agent in registry.list_agents():
        for model in agent.models:
            models.append(
                {
                    "id": model.id,
                    "name": model.name,
                    "description": model.description,
                    "agent_id": agent.id,
                }
            )
    return models


def _resolve_agent_model(
    requested_model: str | None = None,
) -> tuple[str, str | None]:
    requested = (requested_model or "").strip()
    agents = registry.list_agents()

    if requested:
        for agent in agents:
            if requested == agent.id:
                return agent.id, registry.resolve_model(agent.id, None)
            for model in agent.models:
                if requested in {model.id, model.name}:
                    return agent.id, registry.resolve_model(agent.id, model.id)

    agent_id = settings.default_agent
    return agent_id, registry.resolve_model(agent_id, requested or None)


async def _stream_session_events(
    session_id: str,
    text: str,
    request: Request | None = None,
    location: dict | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")

    lock = _get_session_lock(session_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Session is busy")

    agent = registry.get(session["agent_id"])
    store.append_event(session_id=session_id, role="user", content=text)

    async with lock:
        store.set_session_status(session_id, "running")
        collected: list[str] = []
        thinking_collected: list[str] = []
        agent_text = text_with_location_context(text, location)
        yield (
            "status",
            {
                "status": "running",
                "session_id": session_id,
                "created_at": utc_now(),
            },
        )

        binding = store.get_adapter_binding(session_id)
        adapter_state = AdapterState()
        if binding and binding.get("adapter_state_json"):
            try:
                state_data = json.loads(binding["adapter_state_json"])
                adapter_state.native_session_id = state_data.get("native_session_id")
            except Exception:
                pass

        last_persisted_id = adapter_state.native_session_id

        try:
            async for adapter_event in agent.stream_reply_events(
                session_id=session_id,
                text=agent_text,
                model_id=session.get("model_id"),
                workspace_path=session.get("workspace_path"),
                adapter_state=adapter_state,
            ):
                if adapter_state.native_session_id != last_persisted_id:
                    last_persisted_id = adapter_state.native_session_id
                    store.upsert_adapter_binding(
                        session_id=session_id,
                        adapter_name=session["agent_id"],
                        adapter_state_json=json.dumps(
                            {"native_session_id": adapter_state.native_session_id}
                        ),
                        workspace_path=session.get("workspace_path"),
                    )

                if request is not None and await request.is_disconnected():
                    return

                if adapter_event.type == "delta":
                    delta = adapter_event.data.get("delta", "")
                    collected.append(delta)
                    yield ("delta", {"delta": delta})
                elif adapter_event.type == "thinking_delta":
                    delta = adapter_event.data.get("delta", "")
                    if delta:
                        thinking_collected.append(delta)
                        yield ("thinking_delta", adapter_event.data)

            if (
                adapter_state.native_session_id
                and adapter_state.native_session_id != last_persisted_id
            ):
                store.upsert_adapter_binding(
                    session_id=session_id,
                    adapter_name=session["agent_id"],
                    adapter_state_json=json.dumps(
                        {"native_session_id": adapter_state.native_session_id}
                    ),
                    workspace_path=session.get("workspace_path"),
                )

            final_text = "".join(collected).strip()
            thinking_text = "".join(thinking_collected).strip()
            if thinking_text:
                store.append_event(
                    session_id=session_id,
                    role="thinking",
                    content=thinking_text,
                )
            event = store.append_event(
                session_id=session_id,
                role="assistant",
                content=final_text,
            )
            yield (
                "done",
                {
                    "session_id": session_id,
                    "text": final_text,
                    "event": EventResponse(**event).model_dump(),
                },
            )
        except asyncio.CancelledError:
            store.append_event(
                session_id=session_id,
                role="error",
                content="Generation canceled before completion because the client disconnected.",
            )
            raise
        except Exception as exc:
            store.append_event(session_id=session_id, role="error", content=str(exc))
            yield ("error", {"message": str(exc)})
        finally:
            store.set_session_status(session_id, "idle")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(ok=True, service="margaret-gateway")


@app.post("/auth")
async def voice_auth(payload: AuthRequest) -> dict[str, int | str]:
    allowed = {
        token
        for token in (settings.gateway_token, settings.voice_app_secret)
        if token
    }
    if allowed and payload.secret not in allowed:
        raise HTTPException(status_code=401, detail="Unauthorized")
    signing_secret = settings.voice_jwt_secret or payload.secret
    token = create_signed_token(secret=signing_secret)
    return {"token": token, "expires_in": 23 * 60 * 60}


@app.get("/slack/status")
async def slack_status(_: None = Depends(require_auth)) -> dict[str, bool]:
    return {
        "enabled": slack_integration.enabled,
        "running": slack_integration.running,
        "can_start": slack_integration.can_start(),
    }


@app.get("/agents")
async def list_agents(
    _: None = Depends(require_auth),
) -> dict[str, list[AgentResponse]]:
    return {
        "agents": [
            AgentResponse(
                id=agent.id,
                name=agent.name,
                description=agent.description,
                models=[
                    ModelResponse(
                        id=model.id,
                        name=model.name,
                        description=model.description,
                    )
                    for model in agent.models
                ],
                default_model=agent.default_model,
                requires_model=agent.requires_model,
            )
            for agent in registry.list_agents()
        ]
    }


@app.post("/sessions", response_model=SessionResponse)
async def create_session(
    payload: CreateSessionRequest,
    _: None = Depends(require_auth),
) -> SessionResponse:
    agent_id = payload.agent_id or settings.default_agent
    try:
        model_id = registry.resolve_model(agent_id, payload.model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    session = store.create_session(
        agent_id=agent_id,
        model_id=model_id,
        title=payload.title or "Margaret session",
        client=payload.client or "unknown",
        workspace_path=payload.workspace_path,
    )
    return SessionResponse(**session)


@app.get("/sessions", response_model=SessionsResponse)
async def list_sessions(
    days: int = 7,
    _: None = Depends(require_auth),
) -> SessionsResponse:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return SessionsResponse(
        sessions=store.list_sessions(cutoff.isoformat(), include_empty=False)
    )


@app.get("/sessions/{session_id}/history", response_model=HistoryResponse)
async def get_history(
    session_id: str,
    limit: int = 10,
    before_ts: str | None = None,
    _: None = Depends(require_auth),
) -> HistoryResponse:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    return HistoryResponse(
        session_id=session_id,
        messages=store.get_history(session_id, limit=limit, before_ts=before_ts),
    )


@app.post("/routes", response_model=RouteResponse)
async def save_route(
    payload: SaveRouteRequest,
    _: None = Depends(require_auth),
) -> RouteResponse:
    route = store.save_route(payload.model_dump())
    return RouteResponse(**route)


@app.get("/routes", response_model=RoutesResponse)
async def list_routes(
    limit: int = 20,
    _: None = Depends(require_auth),
) -> RoutesResponse:
    return RoutesResponse(routes=store.list_routes(limit=limit))


@app.post("/sessions/{session_id}/messages/stream")
async def stream_message(
    session_id: str,
    payload: SendMessageRequest,
    request: Request,
    _: None = Depends(require_auth),
) -> StreamingResponse:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")

    lock = _get_session_lock(session_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Session is busy")

    async def stream() -> AsyncIterator[str]:
        async for event_type, data in _stream_session_events(
            session_id=session_id,
            text=payload.text,
            request=request,
            location=payload.location.model_dump() if payload.location else None,
        ):
            yield sse_event(event_type, data)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.websocket("/ws")
async def phone_websocket(websocket: WebSocket) -> None:
    handler = PhoneVoiceWebSocketHandler(
        store=store,
        settings=settings,
        stream_events=_stream_session_events,
        resolve_agent_model=_resolve_agent_model,
        voice_service=VoiceService(settings),
    )
    await handler.handle(websocket)
