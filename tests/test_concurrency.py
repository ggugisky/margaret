from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from app.store import Store
import app.main as main
import sqlite3
import time


@pytest.mark.anyio
async def test_concurrent_rejection(tmp_path, monkeypatch):
    db_path = str(tmp_path / "concurrent.sqlite3")
    test_store = Store(db_path)
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "_session_locks", {})

    with TestClient(main.app) as client:
        created = client.post(
            "/sessions",
            json={"agent_id": "echo", "client": "test", "title": "Concurrent"},
        )
        session_id = created.json()["session_id"]

        lock = main._get_session_lock(session_id)
        await lock.acquire()
        try:
            resp = client.post(
                f"/sessions/{session_id}/messages/stream", json={"text": "busy?"}
            )
            assert resp.status_code == 409
            assert resp.json()["detail"] == "Session is busy"
        finally:
            lock.release()

        resp = client.post(
            f"/sessions/{session_id}/messages/stream", json={"text": "available?"}
        )
        assert resp.status_code == 200


def test_stale_recovery(tmp_path):
    db_path = str(tmp_path / "stale.sqlite3")
    store = Store(db_path)

    session = store.create_session("echo", "echo/default", "Stale", "test", None)
    session_id = session["session_id"]

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "update sessions set status = 'running' where session_id = ?", (session_id,)
        )

    s = store.get_session(session_id)
    assert s["status"] == "running"

    store.recover_stale_sessions()

    s = store.get_session(session_id)
    assert s["status"] == "idle"


def test_set_session_status(tmp_path):
    db_path = str(tmp_path / "status.sqlite3")
    store = Store(db_path)
    session = store.create_session("echo", "echo/default", "Status", "test", None)
    session_id = session["session_id"]

    assert store.get_session(session_id)["status"] == "idle"

    store.set_session_status(session_id, "running")
    assert store.get_session(session_id)["status"] == "running"

    store.set_session_status(session_id, "idle")
    assert store.get_session(session_id)["status"] == "idle"
