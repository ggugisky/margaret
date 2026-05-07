from __future__ import annotations

import asyncio
from fastapi.testclient import TestClient  # pyright: ignore[reportMissingImports]

import json
import sqlite3
import app.main as main
from app.adapters import AdapterState, AgentAdapter, AgentInfo
from app.store import Store


def test_health() -> None:
    with TestClient(main.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "service": "margaret-gateway"}


def test_slack_status_endpoint(tmp_path, monkeypatch) -> None:
    test_store = main.Store(str(tmp_path / "gateway.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main.settings, "slack_enabled", False)

    with TestClient(main.app) as client:
        resp = client.get("/slack/status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["enabled"] is False
    assert payload["running"] is False


def test_create_session_and_history(tmp_path, monkeypatch) -> None:
    test_store = main.Store(str(tmp_path / "gateway.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)

    with TestClient(main.app) as client:
        agents = client.get("/agents")
        assert agents.status_code == 200
        echo_agent = agents.json()["agents"][0]
        assert echo_agent["id"] == "echo"
        assert echo_agent["default_model"] == "echo/default"
        assert echo_agent["models"][0]["id"] == "echo/default"

        created = client.post(
            "/sessions",
            json={
                "agent_id": "echo",
                "model_id": "echo/default",
                "client": "test",
                "title": "Test",
            },
        )
        assert created.status_code == 200
        created_payload = created.json()
        session_id = created_payload["session_id"]
        assert created_payload["session_key"] == session_id
        assert created_payload["model_id"] == "echo/default"
        assert created_payload["source_label"] == "test"

        streamed = client.post(
            f"/sessions/{session_id}/messages/stream",
            json={"text": "hello"},
        )
        assert streamed.status_code == 200
        assert "event: delta" in streamed.text
        assert "echo/default" in streamed.text
        assert "event: done" in streamed.text

        history = client.get(f"/sessions/{session_id}/history")
        assert history.status_code == 200
        messages = history.json()["messages"]
        assert [item["role"] for item in messages] == ["user", "assistant"]


def test_unknown_agent_returns_404(tmp_path, monkeypatch) -> None:
    test_store = main.Store(str(tmp_path / "gateway.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)

    with TestClient(main.app) as client:
        resp = client.post("/sessions", json={"agent_id": "missing"})
    assert resp.status_code == 404


def test_unknown_model_returns_404(tmp_path, monkeypatch) -> None:
    test_store = main.Store(str(tmp_path / "gateway.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)

    with TestClient(main.app) as client:
        resp = client.post(
            "/sessions",
            json={"agent_id": "echo", "model_id": "missing-model"},
        )
    assert resp.status_code == 404


def test_old_schema_migration(tmp_path) -> None:
    db_path = str(tmp_path / "old_gateway.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        create table sessions (
            session_id text primary key,
            agent_id text not null,
            title text not null,
            client text not null,
            workspace_path text,
            status text not null,
            created_at text not null,
            updated_at text not null
        )
        """
    )
    conn.execute(
        "insert into sessions values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ses_old",
            "echo",
            "Old Session",
            "test",
            None,
            "idle",
            "2024-01-01T00:00:00Z",
            "2024-01-01T00:00:00Z",
        ),
    )
    conn.execute(
        """
        create table events (
            event_id text primary key,
            session_id text not null,
            role text not null,
            content text not null,
            created_at text not null,
            foreign key(session_id) references sessions(session_id)
        )
        """
    )
    conn.commit()
    conn.close()

    store = Store(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cols = {
        row["name"] for row in conn.execute("pragma table_info(sessions)").fetchall()
    }
    assert "model_id" in cols
    assert "parent_session_id" in cols

    tables = {
        row["name"]
        for row in conn.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    }
    assert "adapter_bindings" in tables
    assert "routes" in tables

    row = conn.execute("select * from sessions where session_id = 'ses_old'").fetchone()
    assert row["title"] == "Old Session"
    conn.close()


def test_routes_api_persists_to_sqlite(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "routes.sqlite3"
    test_store = main.Store(str(db_path))
    monkeypatch.setattr(main, "store", test_store)

    route_payload = {
        "title": "Morning Walk",
        "start_time": "2026-05-06T10:00:00Z",
        "end_time": "2026-05-06T10:10:00Z",
        "duration_sec": 600,
        "distance_m": 321.5,
        "step_count": 459,
        "modes": ["walk"],
        "start_lat": 37.5,
        "start_lng": 127.0,
        "end_lat": 37.501,
        "end_lng": 127.001,
        "points": [
            {"lat": 37.5, "lng": 127.0, "ts": 1778061600000, "mode": "walk"},
            {
                "lat": 37.501,
                "lng": 127.001,
                "ts": 1778062200000,
                "speed": 1.2,
                "mode": "walk",
                "photo_url": "https://example.test/p.jpg",
            },
        ],
    }

    with TestClient(main.app) as client:
        created = client.post("/routes", json=route_payload)
        assert created.status_code == 200
        created_payload = created.json()
        assert created_payload["route_id"].startswith("route:")
        assert created_payload["title"] == "Morning Walk"
        assert created_payload["points"][1]["photo_url"] == "https://example.test/p.jpg"

        listed = client.get("/routes?limit=20")
        assert listed.status_code == 200
        routes = listed.json()["routes"]

    assert len(routes) == 1
    assert routes[0]["route_id"] == created_payload["route_id"]
    assert routes[0]["distance_m"] == 321.5
    assert routes[0]["modes"] == ["walk"]
    assert routes[0]["points"][0]["lat"] == 37.5

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("select * from routes").fetchone()
    assert row is not None
    assert row["title"] == "Morning Walk"
    assert json.loads(row["points_json"])[1]["photo_url"] == "https://example.test/p.jpg"
    conn.close()


def test_adapter_binding_crud(tmp_path) -> None:
    db_path = str(tmp_path / "binding.sqlite3")
    store = Store(db_path)
    session_id = "margaret:test-binding"

    assert store.get_adapter_binding(session_id) is None

    store.upsert_adapter_binding(
        session_id=session_id,
        adapter_name="opencode",
        adapter_state_json='{"native_session_id": "ses_test"}',
        workspace_path="/tmp/test",
    )

    binding = store.get_adapter_binding(session_id)
    assert binding is not None
    assert binding["adapter_name"] == "opencode"
    assert binding["adapter_state_json"] == '{"native_session_id": "ses_test"}'
    assert binding["workspace_path"] == "/tmp/test"
    assert binding["last_used_at"] is not None

    old_last_used = binding["last_used_at"]
    import time

    time.sleep(0.01)
    store.update_binding_last_used(session_id)

    new_binding = store.get_adapter_binding(session_id)
    assert new_binding is not None
    assert new_binding["last_used_at"] > old_last_used


def test_binding_isolation(tmp_path) -> None:
    db_path = str(tmp_path / "isolation.sqlite3")
    store = Store(db_path)
    session_a = "ses-A"
    session_b = "ses-B"

    store.upsert_adapter_binding(
        session_id=session_a,
        adapter_name="echo",
        adapter_state_json='{"native_session_id": "id-A"}',
        workspace_path="/tmp/A",
    )

    binding_b = store.get_adapter_binding(session_b)
    assert binding_b is None

    binding_a = store.get_adapter_binding(session_a)
    assert binding_a is not None
    assert binding_a["adapter_state_json"] == '{"native_session_id": "id-A"}'


def test_api_no_state_leak(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "leak_prevention.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)

    with TestClient(main.app) as client:
        created = client.post(
            "/sessions",
            json={
                "agent_id": "echo",
                "client": "test",
                "title": "Secret Session",
            },
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        test_store.upsert_adapter_binding(
            session_id=session_id,
            adapter_name="echo",
            adapter_state_json='{"native_session_id": "secret-native-id"}',
            workspace_path="/tmp/secret",
        )

        sessions_resp = client.get("/sessions")
        assert sessions_resp.status_code == 200
        sessions_json = sessions_resp.json()
        sessions_str = json.dumps(sessions_json)
        assert "secret-native-id" not in sessions_str
        assert "adapter_state_json" not in sessions_str

        history_resp = client.get(f"/sessions/{session_id}/history")
        assert history_resp.status_code == 200
        history_json = history_resp.json()
        history_str = json.dumps(history_json)
        assert "secret-native-id" not in history_str
        assert "adapter_state_json" not in history_str


def test_sessions_endpoint_hides_empty_sessions(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "hide_empty.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)

    empty = test_store.create_session(
        agent_id="echo",
        model_id="echo/default",
        title="Empty",
        client="voice",
        workspace_path=None,
    )
    non_empty = test_store.create_session(
        agent_id="echo",
        model_id="echo/default",
        title="Non Empty",
        client="voice",
        workspace_path=None,
    )
    test_store.append_event(non_empty["session_id"], "user", "hello")

    with TestClient(main.app) as client:
        resp = client.get("/sessions")

    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert [session["session_id"] for session in sessions] == [non_empty["session_id"]]
    assert empty["session_id"] not in {session["session_id"] for session in sessions}


def test_memory_search_endpoint_uses_rag_memory(monkeypatch) -> None:
    class FakeRagMemory:
        async def search(self, query: str, mode: str = "hybrid") -> str:
            return f"{mode}:{query}"

    monkeypatch.setattr(main, "rag_memory", FakeRagMemory())

    with TestClient(main.app) as client:
        resp = client.get("/memory/search", params={"q": "RAG 계획"})

    assert resp.status_code == 200
    assert resp.json() == {
        "query": "RAG 계획",
        "mode": "hybrid",
        "result": "hybrid:RAG 계획",
    }


def test_memory_search_endpoint_requires_enabled(monkeypatch) -> None:
    monkeypatch.setattr(main, "rag_memory", None)

    with TestClient(main.app) as client:
        resp = client.get("/memory/search", params={"q": "RAG 계획"})

    assert resp.status_code == 503


class _ResumeFailureAdapter(AgentAdapter):
    @property
    def info(self) -> AgentInfo:
        return AgentInfo(id="resume-fail", name="ResumeFail", description="")

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ):
        raise RuntimeError("resume failed")
        yield ""


class _ErrorAfterCaptureAdapter(AgentAdapter):
    @property
    def info(self) -> AgentInfo:
        return AgentInfo(id="error-capture", name="ErrorCapture", description="")

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ):
        state = adapter_state or AdapterState()
        state.native_session_id = "captured-before-error"
        yield "partial delta"
        raise RuntimeError("simulated error after capture")


class _FailOnceThenSucceedAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def info(self) -> AgentInfo:
        return AgentInfo(id="fail-once", name="FailOnce", description="")

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("first stream error")
            yield ""
        yield "second attempt works"


class _SlowCancelAdapter(AgentAdapter):
    @property
    def info(self) -> AgentInfo:
        return AgentInfo(id="slow-cancel", name="SlowCancel", description="")

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ):
        await asyncio.sleep(60)
        yield "too late"


def test_resume_failure_emits_error_and_preserves_old_binding(
    tmp_path, monkeypatch
) -> None:
    test_store = main.Store(str(tmp_path / "resume_failure.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})

    adapter = _ResumeFailureAdapter()
    adapters = dict(main.registry._adapters)
    adapters[adapter.info.id] = adapter
    monkeypatch.setattr(main.registry, "_adapters", adapters)

    with TestClient(main.app) as client:
        created = client.post(
            "/sessions",
            json={
                "agent_id": "resume-fail",
                "client": "test",
                "title": "ResumeFailure",
            },
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        test_store.upsert_adapter_binding(
            session_id=session_id,
            adapter_name="resume-fail",
            adapter_state_json='{"native_session_id": "known-good"}',
            workspace_path=None,
        )

        streamed = client.post(
            f"/sessions/{session_id}/messages/stream",
            json={"text": "continue"},
        )
        assert streamed.status_code == 200
        assert "event: error" in streamed.text
        assert "resume failed" in streamed.text

    binding = test_store.get_adapter_binding(session_id)
    assert binding is not None
    assert binding["adapter_state_json"] == '{"native_session_id": "known-good"}'


def test_capture_before_error_persists_binding_state(tmp_path, monkeypatch) -> None:
    test_store = main.Store(str(tmp_path / "capture_error.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})

    adapter = _ErrorAfterCaptureAdapter()
    adapters = dict(main.registry._adapters)
    adapters[adapter.info.id] = adapter
    monkeypatch.setattr(main.registry, "_adapters", adapters)

    with TestClient(main.app) as client:
        created = client.post(
            "/sessions",
            json={
                "agent_id": "error-capture",
                "client": "test",
                "title": "CaptureBeforeError",
            },
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        streamed = client.post(
            f"/sessions/{session_id}/messages/stream",
            json={"text": "hello"},
        )
        assert streamed.status_code == 200
        assert "event: delta" in streamed.text
        assert "partial delta" in streamed.text
        assert "event: error" in streamed.text
        assert "simulated error after capture" in streamed.text

    binding = test_store.get_adapter_binding(session_id)
    assert binding is not None
    assert (
        binding["adapter_state_json"]
        == '{"native_session_id": "captured-before-error"}'
    )


def test_canceled_stream_persists_error_event(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        test_store = main.Store(str(tmp_path / "cancel_stream.sqlite3"))
        monkeypatch.setattr(main, "store", test_store)
        monkeypatch.setattr(main, "_session_locks", {})

        adapter = _SlowCancelAdapter()
        adapters = dict(main.registry._adapters)
        adapters[adapter.info.id] = adapter
        monkeypatch.setattr(main.registry, "_adapters", adapters)

        session = test_store.create_session(
            agent_id="slow-cancel",
            model_id=None,
            title="Cancel",
            client="test",
            workspace_path=None,
        )

        async def consume() -> None:
            async for _ in main._stream_session_events(
                session["session_id"],
                "long request",
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        history = test_store.get_history(session["session_id"], limit=10)
        assert [item["role"] for item in history] == ["user", "error"]
        assert "client disconnected" in history[-1]["content"]
        assert test_store.get_session(session["session_id"])["status"] == "idle"

    asyncio.run(run())


def test_lock_released_after_stream_error_allows_next_request(
    tmp_path, monkeypatch
) -> None:
    test_store = main.Store(str(tmp_path / "lock_release.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})

    adapter = _FailOnceThenSucceedAdapter()
    adapters = dict(main.registry._adapters)
    adapters[adapter.info.id] = adapter
    monkeypatch.setattr(main.registry, "_adapters", adapters)

    with TestClient(main.app) as client:
        created = client.post(
            "/sessions",
            json={"agent_id": "fail-once", "client": "test", "title": "LockRelease"},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        first = client.post(
            f"/sessions/{session_id}/messages/stream",
            json={"text": "first"},
        )
        assert first.status_code == 200
        assert "event: error" in first.text
        assert "first stream error" in first.text

        second = client.post(
            f"/sessions/{session_id}/messages/stream",
            json={"text": "second"},
        )
        assert second.status_code == 200
        assert "event: delta" in second.text
        assert "second attempt works" in second.text
        assert "event: done" in second.text


def test_has_native_binding_flow(tmp_path, monkeypatch) -> None:
    test_store = Store(str(tmp_path / "binding_flow.sqlite3"))
    monkeypatch.setattr(main, "store", test_store)

    with TestClient(main.app) as client:
        created = client.post(
            "/sessions",
            json={
                "agent_id": "echo",
                "client": "test",
                "title": "Binding Flow",
            },
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        assert created.json()["has_native_binding"] is False
        test_store.append_event(session_id, "user", "hello")

        list_resp = client.get("/sessions")
        assert list_resp.json()["sessions"][0]["has_native_binding"] is False

        test_store.upsert_adapter_binding(
            session_id=session_id,
            adapter_name="echo",
            adapter_state_json='{"native_session_id": "ses_123"}',
            workspace_path="/tmp/test",
        )

        get_resp = client.get(f"/sessions")
        session = get_resp.json()["sessions"][0]
        assert session["session_id"] == session_id
        assert session["has_native_binding"] is True
