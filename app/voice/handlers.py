from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.adapters import AgentInfo, registry
from app.location_context import normalize_location
from app.security import verify_message_signature, verify_signed_token
from app.store import Store
from app.voice.service import VoiceService

StreamEvents = Callable[
    [str, str, Any | None, dict | None], AsyncIterator[tuple[str, dict]]
]
ResolveModel = Callable[[str | None], tuple[str, str | None]]


class PhoneVoiceWebSocketHandler:
    def __init__(
        self,
        *,
        store: Store,
        settings: Any,
        stream_events: StreamEvents,
        resolve_agent_model: ResolveModel,
        voice_service: VoiceService,
    ) -> None:
        self.store = store
        self.settings = settings
        self.stream_events = stream_events
        self.resolve_agent_model = resolve_agent_model
        self.voice_service = voice_service

    def _auth_token_from_ws(self, websocket: WebSocket) -> str:
        auth_header = (
            websocket.headers.get("authorization")
            or websocket.headers.get("Authorization")
            or ""
        )
        token = auth_header.removeprefix("Bearer ").removeprefix("bearer ").strip()
        return token or websocket.query_params.get("token", "").strip()

    def _is_authorized(self, websocket: WebSocket) -> bool:
        allowed = {
            token
            for token in (self.settings.gateway_token, self.settings.voice_app_secret)
            if token
        }
        if not allowed:
            return True
        token = self._auth_token_from_ws(websocket)
        return token in allowed or verify_signed_token(
            token, secret=self.settings.voice_jwt_secret
        )

    def _agent_models_payload(self) -> list[dict[str, str]]:
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

    def _workspace_path(self, requested_name: str | None = None) -> str | None:
        name = (requested_name or self.settings.voice_workspace_name or "").strip()
        if not name:
            return None
        safe_name = name.strip("/").replace("..", "").replace("\\", "/").strip("/")
        if not safe_name:
            return None
        return os.path.join(self.settings.voice_workspace_root, safe_name)

    def _create_session(
        self,
        requested_model: str | None = None,
        workspace_name: str | None = None,
    ) -> tuple[str, str | None]:
        agent_id, model_id = self.resolve_agent_model(requested_model)
        session = self.store.create_session(
            agent_id=agent_id,
            model_id=model_id,
            title="Phone session",
            client="voice",
            workspace_path=self._workspace_path(workspace_name),
        )
        return session["session_id"], model_id

    def _location_from_message(self, msg: dict) -> dict | None:
        direct = normalize_location(msg.get("location") or msg.get("gps"))
        if direct:
            return direct
        return normalize_location(msg)

    async def handle(self, websocket: WebSocket) -> None:
        if not self._is_authorized(websocket):
            await websocket.accept()
            await websocket.send_text(
                json.dumps({"type": "error", "message": "Unauthorized"})
            )
            await websocket.close(code=1008)
            return

        await websocket.accept()

        active_session_id: str | None = None
        pending_model_id = self.resolve_agent_model(None)[1]
        pending_workspace_name: str | None = None

        tts_provider = self.settings.default_tts_provider
        tts_voice = ""
        audio_chunks: list[bytes] = []
        audio_file_ext = "m4a"
        audio_mime_type = "audio/mp4"

        async def send(obj: dict) -> None:
            await websocket.send_text(json.dumps(obj, ensure_ascii=False))

        await send(
            {
                "type": "connected",
                "message": "Margaret Gateway connected",
                "session_key": None,
                "available_backends": ["margaret-gateway"],
                "available_models": self._agent_models_payload(),
                "current_model": pending_model_id,
            }
        )

        async def ensure_active_session() -> tuple[str, str | None]:
            nonlocal active_session_id, pending_model_id, pending_workspace_name
            if active_session_id:
                return active_session_id, pending_model_id
            active_session_id, pending_model_id = self._create_session(
                pending_model_id,
                pending_workspace_name,
            )
            await send(
                {
                    "type": "session_created",
                    "session_key": active_session_id,
                    "current_model": pending_model_id,
                }
            )
            await send({"type": "model_changed", "model": pending_model_id})
            return active_session_id, pending_model_id

        async def run_text_message(text: str, location: dict | None = None) -> None:
            session_id, _ = await ensure_active_session()
            await send({"type": "ack", "text": "알겠습니다", "user_text": text})
            await send({"type": "process_step", "step": "thinking"})
            await send({"type": "thinking"})

            async for event_type, data in self.stream_events(
                session_id,
                text,
                None,
                location,
            ):
                if event_type == "delta":
                    await send({"type": "text_delta", "delta": data.get("delta", "")})
                elif event_type == "thinking_delta":
                    await send({"type": "thinking_delta", "delta": data.get("delta", "")})
                elif event_type == "done":
                    final_text = data.get("text", "")
                    await send({"type": "process_step", "step": "tts_start"})
                    if tts_provider != "off":
                        chunks = await self.voice_service.synthesize_chunks(
                            final_text, tts_provider, tts_voice
                        )
                        total = len(chunks)
                        for index, chunk in enumerate(chunks):
                            payload = {
                                "type": "tts_chunk",
                                "audio": chunk.audio,
                                "index": index,
                                "total": total,
                                "text": chunk.text,
                                "provider": chunk.provider,
                            }
                            if index == 0:
                                payload["full_text"] = final_text
                            await send(payload)
                    await send({"type": "tts_done", "text": final_text})
                    await send({"type": "done", "text": final_text})
                elif event_type == "error":
                    await send({"type": "error", "message": data.get("message", "")})

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await send({"type": "error", "message": "Invalid JSON"})
                    continue
                msg_type = msg.get("type")
                sig = str(msg.pop("_sig", "") or "")
                if msg_type != "ping" and not verify_message_signature(
                    json.dumps(msg, separators=(",", ":"), sort_keys=True),
                    sig,
                    secret=self.settings.voice_msg_hmac_key,
                ):
                    await send({"type": "error", "message": "Invalid signature"})
                    await websocket.close(code=1008)
                    return

                if msg_type == "ping":
                    await send({"type": "pong"})
                    continue

                if msg_type == "new_session":
                    try:
                        active_session_id, pending_model_id = self._create_session(
                            msg.get("model"),
                            msg.get("workspace_name"),
                        )
                        pending_workspace_name = msg.get("workspace_name")
                    except Exception as exc:
                        await send({"type": "error", "message": str(exc)})
                        continue
                    await send(
                        {
                            "type": "session_created",
                            "session_key": active_session_id,
                            "current_model": pending_model_id,
                        }
                    )
                    await send({"type": "model_changed", "model": pending_model_id})
                    continue

                if msg_type == "resume_session":
                    requested_session_id = str(msg.get("session_key") or "").strip()
                    existing = self.store.get_session(requested_session_id)
                    if not existing:
                        await send(
                            {
                                "type": "error",
                                "message": f"Session not found: {requested_session_id}",
                            }
                        )
                        continue
                    active_session_id = requested_session_id
                    pending_model_id = existing.get("model_id")
                    await send(
                        {
                            "type": "session_resumed",
                            "session_key": active_session_id,
                            "message_count": existing.get("message_count", 0),
                            "current_model": existing.get("model_id"),
                        }
                    )
                    if existing.get("model_id"):
                        await send(
                            {"type": "model_changed", "model": existing["model_id"]}
                        )
                    continue

                if msg_type == "set_model":
                    try:
                        requested_model = str(msg.get("model") or "").strip()
                        _, selected_model_id = self.resolve_agent_model(requested_model)
                        pending_model_id = selected_model_id
                        pending_workspace_name = msg.get("workspace_name")
                        active_session_id = None
                    except Exception as exc:
                        await send({"type": "error", "message": str(exc)})
                        continue
                    await send({"type": "model_changed", "model": selected_model_id})
                    continue

                if msg_type == "select_model":
                    try:
                        requested_model = str(msg.get("model") or "").strip()
                        _, selected_model_id = self.resolve_agent_model(requested_model)
                        pending_model_id = selected_model_id
                        pending_workspace_name = msg.get("workspace_name")
                        active_session_id = None
                    except Exception as exc:
                        await send({"type": "error", "message": str(exc)})
                        continue
                    await send({"type": "model_changed", "model": selected_model_id})
                    continue

                if msg_type == "set_ai_backend":
                    await send(
                        {"type": "ai_backend_changed", "backend": "margaret-gateway"}
                    )
                    continue

                if msg_type == "set_tts_provider":
                    tts_provider = msg.get("provider") or "off"
                    tts_voice = msg.get("voice") or ""
                    await send(
                        {
                            "type": "tts_provider_set",
                            "provider": tts_provider,
                            "voice": tts_voice,
                        }
                    )
                    continue

                if msg_type == "cancel_generation":
                    await send({"type": "canceled"})
                    await send({"type": "done", "text": ""})
                    continue

                if msg_type == "text_message":
                    text = str(msg.get("text") or "").strip()
                    if not text:
                        await send(
                            {"type": "error", "message": "텍스트가 비어 있습니다"}
                        )
                        continue
                    await run_text_message(text, self._location_from_message(msg))
                    continue

                if msg_type == "audio_chunk":
                    audio_data = msg.get("audio")
                    if audio_data:
                        audio_chunks.append(base64.b64decode(audio_data))
                    audio_file_ext = msg.get("fileExt") or audio_file_ext
                    audio_mime_type = msg.get("mimeType") or audio_mime_type
                    continue

                if msg_type == "audio_commit":
                    if not audio_chunks:
                        await send({"type": "error", "message": "오디오 데이터가 없습니다"})
                        continue
                    chunks = audio_chunks[:]
                    audio_chunks.clear()
                    await send({"type": "process_step", "step": "stt_start"})
                    user_text = await self.voice_service.speech_to_text(
                        b"".join(chunks),
                        file_ext=audio_file_ext,
                        mime_type=audio_mime_type,
                    )
                    if not user_text.strip():
                        await send({"type": "no_request", "message": "음성이 비어 있습니다"})
                        continue
                    await run_text_message(user_text, self._location_from_message(msg))
                    continue

                await send(
                    {"type": "error", "message": f"Unsupported message type: {msg_type}"}
                )
        except WebSocketDisconnect:
            return
