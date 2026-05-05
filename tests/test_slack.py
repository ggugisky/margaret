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


@dataclass
class _CodexAdapter(AgentAdapter):
    model: str = "gpt-5.5"

    @property
    def info(self) -> AgentInfo:
        return AgentInfo(
            id="codex",
            name="Codex",
            description="",
            models=(ModelInfo(id=self.model, name="GPT-5.5"),),
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
        yield f"codex:{text}"


def _build_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(_DummyAdapter())
    registry.register(_CodexAdapter())
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


def test_slack_user_default_helpers(tmp_path) -> None:
    store = Store(str(tmp_path / "slack_defaults.sqlite3"))

    assert store.get_slack_user_default(team_id="T1", user_id="U1") is None

    store.upsert_slack_user_default(
        team_id="T1",
        user_id="U1",
        agent_id="codex",
        model_id="gpt-5.5",
    )
    default_pref = store.get_slack_user_default(team_id="T1", user_id="U1")
    assert default_pref is not None
    assert default_pref["agent_id"] == "codex"
    assert default_pref["model_id"] == "gpt-5.5"


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
async def test_default_command_saves_preference_without_creating_session(
    tmp_path,
) -> None:
    store = Store(str(tmp_path / "slack_default_cmd.sqlite3"))
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
            "ts": "3000.1",
            "text": "<@B1> default codex gpt-5.5",
        },
        say=say,
    )

    default_pref = store.get_slack_user_default(team_id="T1", user_id="U1")
    assert default_pref is not None
    assert default_pref["agent_id"] == "codex"
    assert default_pref["model_id"] == "gpt-5.5"

    sessions = store.list_sessions("1970-01-01T00:00:00+00:00")
    assert sessions == []
    say.assert_awaited_with(
        text="Saved default for new threads: `codex` / `gpt-5.5`.",
        thread_ts="3000.1",
    )


@pytest.mark.anyio
async def test_new_thread_uses_saved_default(tmp_path) -> None:
    store = Store(str(tmp_path / "slack_saved_default.sqlite3"))
    handler = SlackDMHandler(
        store=store,
        registry=_build_registry(),
        default_agent="dummy",
    )
    say = AsyncMock()

    store.upsert_slack_user_default(
        team_id="T1",
        user_id="U1",
        agent_id="codex",
        model_id="gpt-5.5",
    )

    await handler.handle_message(
        team_id="T1",
        event={
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "ts": "3100.1",
            "text": "hello with saved default",
        },
        say=say,
    )

    session_id = store.get_session_id_by_slack_thread(
        team_id="T1",
        channel_id="D1",
        thread_ts="3100.1",
        user_id="U1",
    )
    assert session_id is not None
    session = store.get_session(session_id)
    assert session is not None
    assert session["agent_id"] == "codex"
    assert session["model_id"] == "gpt-5.5"
    say.assert_awaited_with(text="codex:hello with saved default", thread_ts="3100.1")


@pytest.mark.anyio
async def test_new_thread_explicit_agent_model_creates_session(tmp_path) -> None:
    store = Store(str(tmp_path / "slack_explicit_session.sqlite3"))
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
            "ts": "3200.1",
            "text": "<@B1> codex gpt-5.5",
        },
        say=say,
    )

    session_id = store.get_session_id_by_slack_thread(
        team_id="T1",
        channel_id="D1",
        thread_ts="3200.1",
        user_id="U1",
    )
    assert session_id is not None
    session = store.get_session(session_id)
    assert session is not None
    assert session["agent_id"] == "codex"
    assert session["model_id"] == "gpt-5.5"
    say.assert_awaited_with(
        text="Started new thread with `codex` / `gpt-5.5`.",
        thread_ts="3200.1",
    )


@pytest.mark.anyio
async def test_explicit_command_with_prompt_runs_prompt(tmp_path) -> None:
    store = Store(str(tmp_path / "slack_explicit_prompt.sqlite3"))
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
            "ts": "3300.1",
            "text": "codex gpt-5.5 please summarize this",
        },
        say=say,
    )

    session_id = store.get_session_id_by_slack_thread(
        team_id="T1",
        channel_id="D1",
        thread_ts="3300.1",
        user_id="U1",
    )
    assert session_id is not None
    history = store.get_history(session_id, limit=10)
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "please summarize this"
    say.assert_awaited_with(
        text="codex:please summarize this",
        thread_ts="3300.1",
    )


@pytest.mark.anyio
async def test_existing_thread_rejects_switch_attempt(tmp_path) -> None:
    store = Store(str(tmp_path / "slack_lock.sqlite3"))
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
            "ts": "3400.1",
            "text": "hello",
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
            "thread_ts": "3400.1",
            "ts": "3400.2",
            "text": "codex gpt-5.5 switch now",
        },
        say=say,
    )

    session_id = store.get_session_id_by_slack_thread(
        team_id="T1",
        channel_id="D1",
        thread_ts="3400.1",
        user_id="U1",
    )
    assert session_id is not None
    session = store.get_session(session_id)
    assert session is not None
    assert session["agent_id"] == "dummy"
    assert session["model_id"] == "dummy/default"

    history = store.get_history(session_id, limit=20)
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert say.await_args_list[-1].kwargs == {
        "text": (
            "This thread is locked to `dummy` / `dummy/default`. "
            "Start a new DM thread to switch agent/model."
        ),
        "thread_ts": "3400.1",
    }


@pytest.mark.anyio
async def test_invalid_agent_or_model_returns_safe_message(tmp_path) -> None:
    store = Store(str(tmp_path / "slack_invalid_cmd.sqlite3"))
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
            "ts": "3500.1",
            "text": "default missing gpt-5.5",
        },
        say=say,
    )

    sessions = store.list_sessions("1970-01-01T00:00:00+00:00")
    assert sessions == []
    assert say.await_args is not None
    assert "Invalid agent/model" in say.await_args.kwargs["text"]
    assert "Known agents:" in say.await_args.kwargs["text"]


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
