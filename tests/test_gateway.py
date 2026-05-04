from __future__ import annotations

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
        assert created_payload["model_id"] == "echo/default"

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

    row = conn.execute("select * from sessions where session_id = 'ses_old'").fetchone()
    assert row["title"] == "Old Session"
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
