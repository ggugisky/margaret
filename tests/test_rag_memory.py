from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag_memory import RagMemory


def _make_memory(fake_rag) -> RagMemory:
    fake_rag.initialize_storages = AsyncMock()
    mem = RagMemory.__new__(RagMemory)
    mem._rag = fake_rag
    return mem


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_context_returns_formatted_block_when_results_found() -> None:
    with patch("app.rag_memory._LIGHTRAG_AVAILABLE", True), patch(
        "app.rag_memory.LightRAG"
    ) as MockLightRAG:
        instance = MockLightRAG.return_value
        instance.aquery = AsyncMock(return_value="파랑 보이스 API 인증은 JWT 방식으로 처리했음.")

        mem = _make_memory(instance)

        ctx = await mem.build_context("파랑 보이스 인증")

        assert "[관련 과거 기억]" in ctx
        assert "JWT" in ctx
        assert ctx.endswith("---\n")
        instance.initialize_storages.assert_awaited_once()


@pytest.mark.anyio
async def test_build_context_returns_empty_when_no_results() -> None:
    with patch("app.rag_memory._LIGHTRAG_AVAILABLE", True), patch(
        "app.rag_memory.LightRAG"
    ) as MockLightRAG:
        instance = MockLightRAG.return_value
        instance.aquery = AsyncMock(return_value="")

        mem = _make_memory(instance)

        ctx = await mem.build_context("없는 주제")
        assert ctx == ""


@pytest.mark.anyio
async def test_build_context_returns_empty_on_whitespace_result() -> None:
    with patch("app.rag_memory._LIGHTRAG_AVAILABLE", True), patch(
        "app.rag_memory.LightRAG"
    ) as MockLightRAG:
        instance = MockLightRAG.return_value
        instance.aquery = AsyncMock(return_value="   \n  ")

        mem = _make_memory(instance)

        ctx = await mem.build_context("whitespace")
        assert ctx == ""


@pytest.mark.anyio
async def test_build_context_returns_empty_on_search_exception() -> None:
    with patch("app.rag_memory._LIGHTRAG_AVAILABLE", True), patch(
        "app.rag_memory.LightRAG"
    ) as MockLightRAG:
        instance = MockLightRAG.return_value
        instance.aquery = AsyncMock(side_effect=RuntimeError("rag 오류"))

        mem = _make_memory(instance)

        ctx = await mem.build_context("쿼리")
        assert ctx == ""


@pytest.mark.anyio
async def test_build_context_returns_empty_on_initialize_exception() -> None:
    with patch("app.rag_memory._LIGHTRAG_AVAILABLE", True), patch(
        "app.rag_memory.LightRAG"
    ) as MockLightRAG:
        instance = MockLightRAG.return_value
        instance.initialize_storages = AsyncMock(side_effect=RuntimeError("init 오류"))
        instance.aquery = AsyncMock(return_value="should not run")

        mem = RagMemory.__new__(RagMemory)
        mem._rag = instance

        ctx = await mem.build_context("쿼리")
        assert ctx == ""
        instance.aquery.assert_not_called()


# ---------------------------------------------------------------------------
# index_event
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_index_event_skips_error_role() -> None:
    with patch("app.rag_memory._LIGHTRAG_AVAILABLE", True), patch(
        "app.rag_memory.LightRAG"
    ) as MockLightRAG:
        instance = MockLightRAG.return_value
        instance.ainsert = AsyncMock()

        mem = _make_memory(instance)

        await mem.index_event("session-1", "error", "some error")
        instance.ainsert.assert_not_called()


@pytest.mark.anyio
async def test_index_event_skips_empty_content() -> None:
    with patch("app.rag_memory._LIGHTRAG_AVAILABLE", True), patch(
        "app.rag_memory.LightRAG"
    ) as MockLightRAG:
        instance = MockLightRAG.return_value
        instance.ainsert = AsyncMock()

        mem = _make_memory(instance)

        await mem.index_event("session-1", "user", "   ")
        instance.ainsert.assert_not_called()


@pytest.mark.anyio
async def test_index_event_inserts_with_session_tag() -> None:
    with patch("app.rag_memory._LIGHTRAG_AVAILABLE", True), patch(
        "app.rag_memory.LightRAG"
    ) as MockLightRAG:
        instance = MockLightRAG.return_value
        instance.ainsert = AsyncMock()

        mem = _make_memory(instance)

        await mem.index_event("session-abc", "user", "안녕하세요")
        instance.ainsert.assert_called_once()
        call_arg = instance.ainsert.call_args[0][0]
        assert "session-abc" in call_arg
        assert "안녕하세요" in call_arg
        instance.initialize_storages.assert_awaited_once()


@pytest.mark.anyio
async def test_index_event_swallows_initialize_exception() -> None:
    with patch("app.rag_memory._LIGHTRAG_AVAILABLE", True), patch(
        "app.rag_memory.LightRAG"
    ) as MockLightRAG:
        instance = MockLightRAG.return_value
        instance.initialize_storages = AsyncMock(side_effect=RuntimeError("init 오류"))
        instance.ainsert = AsyncMock()

        mem = RagMemory.__new__(RagMemory)
        mem._rag = instance

        await mem.index_event("session-abc", "user", "안녕하세요")
        instance.ainsert.assert_not_called()


# ---------------------------------------------------------------------------
# SlackDMHandler — RAG 컨텍스트 주입
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_slack_handler_injects_rag_context_into_agent(tmp_path) -> None:
    """에이전트에게 전달되는 텍스트에 RAG 컨텍스트가 앞에 붙는지 확인."""
    from dataclasses import dataclass

    from app.adapters import AgentAdapter, AgentInfo, AgentRegistry, ModelInfo
    from app.slack.handlers import SlackDMHandler
    from app.store import Store

    received_texts: list[str] = []

    @dataclass
    class _CapturingAdapter(AgentAdapter):
        @property
        def info(self) -> AgentInfo:
            return AgentInfo(
                id="dummy",
                name="Dummy",
                description="",
                models=(ModelInfo(id="dummy/default", name="Dummy Default"),),
                default_model="dummy/default",
                requires_model=False,
            )

        async def stream_reply(self, session_id, text, model_id, workspace_path=None, adapter_state=None):
            received_texts.append(text)
            yield "ok"

    registry = AgentRegistry()
    registry.register(_CapturingAdapter())

    store = Store(str(tmp_path / "test.sqlite3"))
    session = store.create_session(
        agent_id="dummy",
        model_id="dummy/default",
        title="test",
        client="test",
        workspace_path=None,
    )
    session_id = session["session_id"]

    fake_rag = MagicMock()
    fake_rag.build_context = AsyncMock(return_value="[관련 과거 기억]\n예전 대화 내용\n---\n")
    fake_rag.index_event = AsyncMock()

    handler = SlackDMHandler(
        store=store,
        registry=registry,
        default_agent="dummy",
        rag_memory=fake_rag,
    )

    await handler._run_agent_turn(session_id=session_id, text="테스트 질문")

    assert len(received_texts) == 1
    assert received_texts[0].startswith("[관련 과거 기억]")
    assert "테스트 질문" in received_texts[0]


@pytest.mark.anyio
async def test_slack_handler_no_injection_when_rag_returns_empty(tmp_path) -> None:
    """RAG가 빈 컨텍스트를 반환하면 원본 텍스트 그대로 전달."""
    from dataclasses import dataclass

    from app.adapters import AgentAdapter, AgentInfo, AgentRegistry, ModelInfo
    from app.slack.handlers import SlackDMHandler
    from app.store import Store

    received_texts: list[str] = []

    @dataclass
    class _CapturingAdapter(AgentAdapter):
        @property
        def info(self) -> AgentInfo:
            return AgentInfo(
                id="dummy",
                name="Dummy",
                description="",
                models=(ModelInfo(id="dummy/default", name="Dummy Default"),),
                default_model="dummy/default",
                requires_model=False,
            )

        async def stream_reply(self, session_id, text, model_id, workspace_path=None, adapter_state=None):
            received_texts.append(text)
            yield "ok"

    registry = AgentRegistry()
    registry.register(_CapturingAdapter())

    store = Store(str(tmp_path / "test.sqlite3"))
    session = store.create_session(
        agent_id="dummy",
        model_id="dummy/default",
        title="test",
        client="test",
        workspace_path=None,
    )
    session_id = session["session_id"]

    fake_rag = MagicMock()
    fake_rag.build_context = AsyncMock(return_value="")
    fake_rag.index_event = AsyncMock()

    handler = SlackDMHandler(
        store=store,
        registry=registry,
        default_agent="dummy",
        rag_memory=fake_rag,
    )

    await handler._run_agent_turn(session_id=session_id, text="원본 질문")

    assert received_texts[0] == "원본 질문"
