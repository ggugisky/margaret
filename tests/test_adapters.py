import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.adapters import (
    AdapterState,
    CodexAgentAdapter,
    OpenCodeAgentAdapter,
    ClaudeCodeAgentAdapter,
    CopilotAgentAdapter,
)


async def make_mock_proc(lines: list[str], returncode: int = 0):
    mock_stdout = AsyncMock()
    encoded = [l.encode() + b"\n" for l in lines] + [b""]
    mock_stdout.readline = AsyncMock(side_effect=encoded)

    mock_proc = MagicMock()
    mock_proc.stdout = mock_stdout
    mock_proc.returncode = returncode
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    return mock_proc


@pytest.mark.anyio
async def test_codex_adapter_parsing():
    adapter = CodexAgentAdapter()
    state = AdapterState()
    lines = [
        '{"type":"thread.started","thread_id":"codex-thread-1"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"hello world"}}',
        '{"type":"turn.completed","usage":{}}',
    ]

    with patch(
        "asyncio.create_subprocess_exec", return_value=await make_mock_proc(lines)
    ):
        deltas = []
        async for delta in adapter.stream_reply(
            "ses_test", "hello", "gpt-5.5", adapter_state=state
        ):
            deltas.append(delta)

    assert "".join(deltas) == "hello world"
    assert state.native_session_id == "codex-thread-1"


@pytest.mark.anyio
async def test_opencode_adapter_parsing():
    adapter = OpenCodeAgentAdapter()
    state = AdapterState()
    lines = [
        '{"type":"step_start","sessionID":"oc-session-1","part":{}}',
        '{"type":"text","sessionID":"oc-session-1","part":{"type":"text","text":"hello world"}}',
        '{"type":"step_finish","sessionID":"oc-session-1","part":{}}',
    ]

    with patch(
        "asyncio.create_subprocess_exec", return_value=await make_mock_proc(lines)
    ):
        deltas = []
        async for delta in adapter.stream_reply(
            "ses_test",
            "hello",
            "amazon-bedrock/anthropic.claude-sonnet-4-6",
            adapter_state=state,
        ):
            deltas.append(delta)

    assert "".join(deltas) == "hello world"
    assert state.native_session_id == "oc-session-1"


@pytest.mark.anyio
async def test_opencode_adapter_resume_flag():
    adapter = OpenCodeAgentAdapter()
    state = AdapterState(native_session_id="ses_existing123")
    lines = [
        '{"type":"text","sessionID":"ses_existing123","part":{"type":"text","text":"resumed"}}',
    ]

    with patch(
        "asyncio.create_subprocess_exec", return_value=await make_mock_proc(lines)
    ) as mock_exec:
        deltas = []
        async for delta in adapter.stream_reply(
            "ses_test",
            "follow up",
            "amazon-bedrock/anthropic.claude-sonnet-4-6",
            adapter_state=state,
        ):
            deltas.append(delta)

    assert "".join(deltas) == "resumed"
    cmd_args = mock_exec.call_args[0]
    assert "--session" in cmd_args
    idx = cmd_args.index("--session")
    assert cmd_args[idx + 1] == "ses_existing123"


@pytest.mark.anyio
async def test_opencode_adapter_error_event():
    adapter = OpenCodeAgentAdapter()
    state = AdapterState()
    lines = [
        '{"type":"error","sessionID":"oc-err-1","error":{"message":"session not found"}}',
    ]

    with patch(
        "asyncio.create_subprocess_exec", return_value=await make_mock_proc(lines)
    ):
        with pytest.raises(RuntimeError, match="session not found"):
            async for _ in adapter.stream_reply(
                "ses_test",
                "hello",
                "amazon-bedrock/anthropic.claude-sonnet-4-6",
                adapter_state=state,
            ):
                pass


@pytest.mark.anyio
async def test_claudecode_adapter_parsing():
    adapter = ClaudeCodeAgentAdapter()
    state = AdapterState()
    lines = [
        '{"type":"system","subtype":"init","session_id":"claude-session-1","tools":[],"mcp_servers":[]}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hello world"}]}}',
        '{"type":"result","subtype":"success","result":"hello world","session_id":"claude-session-1"}',
    ]

    with patch(
        "asyncio.create_subprocess_exec", return_value=await make_mock_proc(lines)
    ):
        deltas = []
        async for delta in adapter.stream_reply(
            "ses_test", "hello", "sonnet", adapter_state=state
        ):
            deltas.append(delta)

    assert "".join(deltas) == "hello world"
    assert state.native_session_id == "claude-session-1"


@pytest.mark.anyio
async def test_copilot_adapter_parsing():
    adapter = CopilotAgentAdapter()
    state = AdapterState()
    lines = [
        '{"type":"assistant.message","data":{"content":"hello world"}}',
        '{"type":"result","sessionId":"copilot-session-1","exitCode":0}',
    ]

    with patch(
        "asyncio.create_subprocess_exec", return_value=await make_mock_proc(lines)
    ):
        deltas = []
        async for delta in adapter.stream_reply(
            "ses_test", "hello", "gpt-5.2", adapter_state=state
        ):
            deltas.append(delta)

    assert "".join(deltas) == "hello world"
    assert state.native_session_id == "copilot-session-1"


@pytest.mark.anyio
async def test_adapter_error_handling():
    adapter = CodexAgentAdapter()
    lines = ['{"type":"error","message":"Something went wrong"}']

    with patch(
        "asyncio.create_subprocess_exec", return_value=await make_mock_proc(lines)
    ):
        with pytest.raises(RuntimeError, match="Something went wrong"):
            async for _ in adapter.stream_reply("ses_test", "hello", "gpt-5.5"):
                pass
