from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest  # pyright: ignore[reportMissingImports]

from app.adapters import AgentAdapter, AgentInfo, AgentRegistry, ModelInfo
from app.config import Settings
from app.slack.handlers import SlackDMHandler
from app.store import Store


@dataclass
class _DummyAdapter(AgentAdapter):
    model: str = "dummy/default"

    @property
    def info(self) -> AgentInfo:
        return AgentInfo(
            id="dummy",
            name="Dummy",
            description="",
            models=(ModelInfo(id=self.model, name="Dummy Default"),),
            default_model=self.model,
            requires_model=False,
        )

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state=None,
    ):
        yield f"dummy:{text}"


def _build_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(_DummyAdapter())
    return registry


def test_settings_slack_enabled_parsing(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_ENABLED", "true")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    cfg = Settings()
    assert cfg.slack_enabled is True
    assert cfg.slack_app_token == "xapp-test"
    assert cfg.slack_bot_token == "xoxb-test"


def test_settings_slack_disabled_default(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_ENABLED", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    cfg = Settings()
    assert cfg.slack_enabled is False
    assert cfg.slack_app_token == ""
    assert cfg.slack_bot_token == ""


def test_slack_thread_mapping_helpers(tmp_path) -> None:
    store = Store(str(tmp_path / "slack_map.sqlite3"))
    session = store.create_session("dummy", "dummy/default", "Slack", "slack", None)
    session_id = session["session_id"]

    store.upsert_slack_thread_mapping(
        team_id="T1",
        channel_id="D1",
        thread_ts="111.222",
        user_id="U1",
        session_id=session_id,
    )

    mapping = store.get_slack_thread_mapping(
        team_id="T1",
        channel_id="D1",
        thread_ts="111.222",
        user_id="U1",
    )
    assert mapping is not None
    assert mapping["session_id"] == session_id
    assert (
        store.get_session_id_by_slack_thread(
            team_id="T1",
            channel_id="D1",
            thread_ts="111.222",
            user_id="U1",
        )
        == session_id
    )


@pytest.mark.anyio
async def test_dm_message_creates_and_reuses_session(tmp_path) -> None:
    store = Store(str(tmp_path / "slack_handler.sqlite3"))
    handler = SlackDMHandler(
        store=store,
        registry=_build_registry(),
        default_agent="dummy",
    )
    say = AsyncMock()

    await handler.handle_message(
        team_id="T1",
        event={
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "ts": "1000.1",
            "text": "hello",
        },
        say=say,
    )

    first_session_id = store.get_session_id_by_slack_thread(
        team_id="T1",
        channel_id="D1",
        thread_ts="1000.1",
        user_id="U1",
    )
    assert first_session_id is not None
    say.assert_awaited_with(text="dummy:hello", thread_ts="1000.1")

    await handler.handle_message(
        team_id="T1",
        event={
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "thread_ts": "1000.1",
            "ts": "1000.2",
            "text": "again",
        },
        say=say,
    )

    reused_session_id = store.get_session_id_by_slack_thread(
        team_id="T1",
        channel_id="D1",
        thread_ts="1000.1",
        user_id="U1",
    )
    assert reused_session_id == first_session_id

    sessions = store.list_sessions("1970-01-01T00:00:00+00:00")
    assert len(sessions) == 1
    history = store.get_history(first_session_id, limit=20)
    assert [item["role"] for item in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


@pytest.mark.anyio
async def test_non_dm_or_bot_messages_are_ignored(tmp_path) -> None:
    store = Store(str(tmp_path / "slack_ignore.sqlite3"))
    handler = SlackDMHandler(
        store=store,
        registry=_build_registry(),
        default_agent="dummy",
    )
    say = AsyncMock()

    await handler.handle_message(
        team_id="T1",
        event={
            "type": "message",
            "channel_type": "channel",
            "channel": "C1",
            "user": "U1",
            "ts": "2000.1",
            "text": "ignore me",
        },
        say=say,
    )
    await handler.handle_message(
        team_id="T1",
        event={
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "bot_id": "B1",
            "ts": "2000.2",
            "text": "bot",
        },
        say=say,
    )

    say.assert_not_awaited()
    sessions = store.list_sessions("1970-01-01T00:00:00+00:00")
    assert sessions == []
