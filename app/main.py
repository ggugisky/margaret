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
)
from fastapi.responses import StreamingResponse  # pyright: ignore[reportMissingImports]

from app.adapters import AdapterState, registry
from app.config import settings
from app.models import (
    AgentResponse,
    CreateSessionRequest,
    EventResponse,
    HealthResponse,
    HistoryResponse,
    SendMessageRequest,
    SessionResponse,
    SessionsResponse,
    ModelResponse,
)
from app.store import Store, utc_now
from app.slack import SlackDMHandler, SlackIntegration


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
    if not settings.gateway_token:
        return
    expected = f"Bearer {settings.gateway_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(ok=True, service="margaret-gateway")


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
    return SessionsResponse(sessions=store.list_sessions(cutoff.isoformat()))


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

    agent = registry.get(session["agent_id"])
    store.append_event(session_id=session_id, role="user", content=payload.text)

    async def stream() -> AsyncIterator[str]:
        async with lock:
            store.set_session_status(session_id, "running")
            collected: list[str] = []
            yield sse_event(
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
                    adapter_state.native_session_id = state_data.get(
                        "native_session_id"
                    )
                except Exception:
                    pass

            _last_persisted_id = adapter_state.native_session_id

            try:
                async for delta in agent.stream_reply(
                    session_id=session_id,
                    text=payload.text,
                    model_id=session.get("model_id"),
                    workspace_path=session.get("workspace_path"),
                    adapter_state=adapter_state,
                ):
                    if adapter_state.native_session_id != _last_persisted_id:
                        _last_persisted_id = adapter_state.native_session_id
                        store.upsert_adapter_binding(
                            session_id=session_id,
                            adapter_name=session["agent_id"],
                            adapter_state_json=json.dumps(
                                {
                                    "native_session_id": adapter_state.native_session_id,
                                }
                            ),
                            workspace_path=session.get("workspace_path"),
                        )

                    if await request.is_disconnected():
                        return

                    collected.append(delta)
                    yield sse_event("delta", {"delta": delta})

                if (
                    adapter_state.native_session_id
                    and adapter_state.native_session_id != _last_persisted_id
                ):
                    _last_persisted_id = adapter_state.native_session_id
                    store.upsert_adapter_binding(
                        session_id=session_id,
                        adapter_name=session["agent_id"],
                        adapter_state_json=json.dumps(
                            {"native_session_id": adapter_state.native_session_id}
                        ),
                        workspace_path=session.get("workspace_path"),
                    )

                final_text = "".join(collected).strip()
                event = store.append_event(
                    session_id=session_id,
                    role="assistant",
                    content=final_text,
                )
                yield sse_event(
                    "done",
                    {
                        "session_id": session_id,
                        "text": final_text,
                        "event": EventResponse(**event).model_dump(),
                    },
                )
            except Exception as exc:
                store.append_event(
                    session_id=session_id, role="error", content=str(exc)
                )
                yield sse_event("error", {"message": str(exc)})
            finally:
                store.set_session_status(session_id, "idle")

    return StreamingResponse(stream(), media_type="text/event-stream")
