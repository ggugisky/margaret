from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main
from app.adapters import AgentAdapter, AgentInfo, ModelInfo
from app.store import Store
from app.voice.service import TtsChunk


def _receive_until(ws, expected_type: str, limit: int = 30) -> list[dict]:
    messages: list[dict] = []
    for _ in range(limit):
        msg = ws.receive_json()
        messages.append(msg)
        if msg.get("type") == expected_type:
            return messages
    raise AssertionError(f"Did not receive {expected_type}: {messages}")


def test_e2e_ws_phone_ok(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_phone.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "slack_enabled", False)
    monkeypatch.setattr(main.settings, "default_tts_provider", "off")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            connected = ws.receive_json()
            assert connected["type"] == "connected"
            assert connected["session_key"] is None
            assert connected["available_backends"] == ["margaret-gateway"]
            assert connected["current_model"] == "echo/default"

            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}

            ws.send_json({"type": "text_message", "text": "phone hello"})
            messages = _receive_until(ws, "done", limit=50)

    message_types = [msg["type"] for msg in messages]
    assert message_types[:5] == [
        "session_created",
        "model_changed",
        "ack",
        "process_step",
        "thinking",
    ]
    assert "text_delta" in message_types
    assert "tts_done" in message_types
    assert messages[-1]["text"].endswith("phone hello")

    session_key = messages[0]["session_key"]
    assert session_key.startswith("margaret:")
    history = test_store.get_history(session_key, limit=10)
    assert [item["role"] for item in history] == ["user", "assistant"]


def test_ws_rejects_when_token_required(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_auth.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "slack_enabled", False)
    monkeypatch.setattr(main.settings, "gateway_token", "secret-token")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "secret-token")

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            assert msg == {"type": "error", "message": "Unauthorized"}

    monkeypatch.setattr(main.settings, "gateway_token", "")


def test_ws_accepts_query_token_when_required(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_auth_ok.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "slack_enabled", False)
    monkeypatch.setattr(main.settings, "gateway_token", "secret-token")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "secret-token")

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws?token=secret-token") as ws:
            assert ws.receive_json()["type"] == "connected"

    monkeypatch.setattr(main.settings, "gateway_token", "")


def test_ws_connect_does_not_create_empty_session(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_no_empty.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            connected = ws.receive_json()
            assert connected["type"] == "connected"
            assert connected["session_key"] is None

    assert test_store.list_sessions("1970-01-01T00:00:00+00:00") == []


def test_ws_model_select_is_deferred_until_message(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_model_deferred.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "default_tts_provider", "off")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "select_model", "model": "echo/default"})
            assert ws.receive_json() == {
                "type": "model_changed",
                "model": "echo/default",
            }
            assert test_store.list_sessions("1970-01-01T00:00:00+00:00") == []

            ws.send_json({"type": "text_message", "text": "phone hello"})
            messages = _receive_until(ws, "done", limit=50)

    session_created = next(msg for msg in messages if msg["type"] == "session_created")
    session = test_store.get_session(session_created["session_key"])
    assert session is not None
    assert session["model_id"] == "echo/default"


def test_ws_text_message_emits_tts_chunk(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_tts.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "default_tts_provider", "openai-hd")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")

    class FakeVoiceService:
        def __init__(self, settings) -> None:
            pass

        async def synthesize_chunks(self, text, preferred_provider, voice):
            return [TtsChunk(audio="ZHVtbXk=", provider="fake", text=text)]

    monkeypatch.setattr(main, "VoiceService", FakeVoiceService)

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "text_message", "text": "phone hello"})
            messages = _receive_until(ws, "done", limit=50)

    message_types = [msg["type"] for msg in messages]
    assert "tts_chunk" in message_types
    tts_chunk = next(msg for msg in messages if msg["type"] == "tts_chunk")
    assert tts_chunk["audio"] == "ZHVtbXk="
    assert tts_chunk["provider"] == "fake"


def test_ws_text_message_returns_text_when_tts_fails(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_tts_failure.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "default_tts_provider", "openai-hd")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")

    class FakeVoiceService:
        def __init__(self, settings) -> None:
            pass

        async def synthesize_chunks(self, text, preferred_provider, voice):
            raise RuntimeError("insufficient_quota")

    monkeypatch.setattr(main, "VoiceService", FakeVoiceService)

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "text_message", "text": "phone hello"})
            messages = _receive_until(ws, "done", limit=50)

    message_types = [msg["type"] for msg in messages]
    assert "error" in message_types
    assert next(msg for msg in messages if msg["type"] == "error")["message"].startswith(
        "TTS 오류:"
    )
    assert messages[-1]["type"] == "done"
    assert messages[-1]["text"].endswith("phone hello")


def test_ws_text_message_passes_location_context_to_agent(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_location.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "default_tts_provider", "off")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            connected = ws.receive_json()
            ws.send_json(
                {
                    "type": "text_message",
                    "text": "nearby coffee?",
                    "location": {"lat": 37.5, "lng": 127.0, "accuracy": 12},
                }
            )
            messages = _receive_until(ws, "done", limit=50)

    assert "Voice GPS context" in messages[-1]["text"]
    assert "latitude: 37.5" in messages[-1]["text"]
    assert "accuracy_m: 12" in messages[-1]["text"]
    assert connected["session_key"] is None
    session_key = next(
        msg["session_key"] for msg in messages if msg["type"] == "session_created"
    )
    history = test_store.get_history(session_key, limit=10)
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "nearby coffee?"


def test_ws_text_message_sends_linked_markdown_document(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_markdown.sqlite3"))
    workspace_root = tmp_path / "workspace"
    workspace = workspace_root / "phone"
    workspace.mkdir(parents=True)
    doc_path = workspace / "notes.md"
    doc_path.write_text("# Notes\n\nPhone-readable content.\n", encoding="utf-8")

    class MarkdownLinkAdapter(AgentAdapter):
        @property
        def info(self) -> AgentInfo:
            return AgentInfo(
                id="md-link",
                name="Markdown Link",
                description="",
                models=(ModelInfo(id="md-link/default", name="Default"),),
                default_model="md-link/default",
            )

        async def stream_reply(
            self,
            session_id,
            text,
            model_id,
            workspace_path=None,
            adapter_state=None,
        ):
            yield "문서는 [Notes](notes.md) 에 있어요."

    adapters = dict(main.registry._adapters)
    adapters["md-link"] = MarkdownLinkAdapter()
    monkeypatch.setattr(main.registry, "_adapters", adapters)
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})
    monkeypatch.setattr(main.settings, "default_agent", "md-link")
    monkeypatch.setattr(main.settings, "default_tts_provider", "off")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")
    monkeypatch.setattr(main.settings, "workspace_root", str(workspace_root))
    monkeypatch.setattr(main.settings, "voice_workspace_root", str(workspace_root))
    monkeypatch.setattr(main.settings, "voice_workspace_name", "phone")

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "text_message", "text": "show doc"})
            messages = _receive_until(ws, "done", limit=50)

    doc_msg = next(msg for msg in messages if msg["type"] == "markdown_document")
    document = doc_msg["document"]
    assert document["title"] == "Notes"
    assert document["filename"] == "notes.md"
    assert document["mime_type"] == "text/markdown"
    assert "Phone-readable content." in document["content"]
    assert messages[-1]["documents"][0]["content"] == document["content"]


def test_ws_audio_commit_runs_stt_and_tts(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_audio.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "default_tts_provider", "openai-hd")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")

    class FakeVoiceService:
        def __init__(self, settings) -> None:
            self.audio_seen = b""

        async def speech_to_text(self, audio_bytes, **kwargs):
            self.audio_seen = audio_bytes
            return "audio hello"

        async def synthesize_chunks(self, text, preferred_provider, voice):
            return [TtsChunk(audio="ZHVtbXk=", provider="fake", text=text)]

    monkeypatch.setattr(main, "VoiceService", FakeVoiceService)

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            connected = ws.receive_json()
            ws.send_json(
                {
                    "type": "audio_chunk",
                    "audio": "ZHVtbXk=",
                    "fileExt": "m4a",
                    "mimeType": "audio/mp4",
                }
            )
            ws.send_json({"type": "audio_commit"})
            messages = _receive_until(ws, "done")

    message_types = [msg["type"] for msg in messages]
    assert "tts_chunk" in message_types
    assert connected["session_key"] is None
    session_key = next(
        msg["session_key"] for msg in messages if msg["type"] == "session_created"
    )
    history = test_store.get_history(session_key, limit=10)
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "audio hello"


def test_ws_audio_commit_stt_failure_keeps_socket_open(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_stt_failure.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "default_tts_provider", "off")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")

    class FakeVoiceService:
        def __init__(self, settings) -> None:
            pass

        async def speech_to_text(self, audio_bytes, **kwargs):
            raise RuntimeError("insufficient_quota")

    monkeypatch.setattr(main, "VoiceService", FakeVoiceService)

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "audio_chunk",
                    "audio": "ZHVtbXk=",
                    "fileExt": "m4a",
                    "mimeType": "audio/mp4",
                }
            )
            ws.send_json({"type": "audio_commit"})
            messages = _receive_until(ws, "error", limit=10)
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()

    assert messages[-1]["message"].startswith("STT 오류:")
    assert pong == {"type": "pong"}


def test_ws_new_session_uses_workspace_name(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "ws_workspace.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})
    monkeypatch.setattr(main.settings, "default_agent", "echo")
    monkeypatch.setattr(main.settings, "default_tts_provider", "off")
    monkeypatch.setattr(main.settings, "gateway_token", "")
    monkeypatch.setattr(main.settings, "voice_app_secret", "")
    monkeypatch.setattr(main.settings, "voice_jwt_secret", "")
    monkeypatch.setattr(main.settings, "voice_msg_hmac_key", "")
    workspace_root = tmp_path / "workspace"
    monkeypatch.setattr(main.settings, "voice_workspace_root", str(workspace_root))

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "type": "new_session",
                    "model": "echo/default",
                    "workspace_name": "ggugisky",
                }
            )
            msg = ws.receive_json()

    assert msg["type"] == "session_created"
    session = test_store.get_session(msg["session_key"])
    assert session is not None
    assert session["client"] == "voice"
    assert session["workspace_path"] == str(workspace_root / "ggugisky")
    assert (workspace_root / "ggugisky").is_dir()
