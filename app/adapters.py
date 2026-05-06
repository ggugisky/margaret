from __future__ import annotations

import asyncio
import json
import logging
import shutil
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class AgentInfo:
    id: str
    name: str
    description: str
    models: tuple[ModelInfo, ...] = ()
    default_model: str | None = None
    requires_model: bool = False


@dataclass
class AdapterState:
    native_session_id: str | None = None


@dataclass(frozen=True)
class AgentStreamEvent:
    type: str
    data: dict[str, Any]


class AgentAdapter(ABC):
    @property
    @abstractmethod
    def info(self) -> AgentInfo:
        raise NotImplementedError

    @abstractmethod
    def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ) -> AsyncIterator[str]:
        raise NotImplementedError

    async def stream_reply_events(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        async for delta in self.stream_reply(
            session_id=session_id,
            text=text,
            model_id=model_id,
            workspace_path=workspace_path,
            adapter_state=adapter_state,
        ):
            yield AgentStreamEvent("delta", {"delta": delta})


# ---------------------------------------------------------------------------
# Echo adapter (development)
# ---------------------------------------------------------------------------


class EchoAgentAdapter(AgentAdapter):
    @property
    def info(self) -> AgentInfo:
        return AgentInfo(
            id="echo",
            name="Echo",
            description="Development adapter that streams back the user message.",
            models=(
                ModelInfo(
                    id="echo/default",
                    name="Echo Default",
                    description="Deterministic development model.",
                ),
            ),
            default_model="echo/default",
            requires_model=False,
        )

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ) -> AsyncIterator[str]:
        model_label = model_id or self.info.default_model or "unknown-model"
        reply = f"Margaret received your request in {session_id} using {model_label}: {text}"
        for token in reply.split(" "):
            await asyncio.sleep(0.01)
            yield token + " "


# ---------------------------------------------------------------------------
# Codex CLI adapter
# ---------------------------------------------------------------------------

_CODEX_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        id="gpt-5.5", name="GPT-5.5", description="OpenAI GPT-5.5 frontier model."
    ),
    ModelInfo(id="gpt-5.4", name="GPT-5.4", description="OpenAI GPT-5.4."),
    ModelInfo(id="o3", name="o3", description="OpenAI o3 reasoning model."),
)

_CODEX_DEFAULT_MODEL = "gpt-5.5"


def _collect_text_fragments(value: Any) -> list[str]:
    fragments: list[str] = []
    if value is None:
        return fragments
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        for item in value:
            fragments.extend(_collect_text_fragments(item))
        return fragments
    if isinstance(value, dict):
        for key in ("text", "content", "summary", "output", "message", "command"):
            if key in value:
                fragments.extend(_collect_text_fragments(value[key]))
        return fragments
    return fragments


def _format_codex_progress_item(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "item")
    if item_type == "agent_message":
        return ""

    if item_type in {"tool_call", "function_call"}:
        name = item.get("name") or item.get("tool_name") or item.get("call_id") or "tool"
        args = item.get("arguments") or item.get("args") or item.get("input") or ""
        args_text = " ".join(_collect_text_fragments(args)) if not isinstance(args, str) else args
        return f"[{item_type}] {name} {args_text}".strip()

    text = " ".join(_collect_text_fragments(item))
    if not text:
        return ""
    return f"[{item_type}] {text}".strip()


class CodexAgentAdapter(AgentAdapter):
    """Adapter that wraps ``codex exec --json`` (non-interactive JSONL mode)."""

    @property
    def info(self) -> AgentInfo:
        return AgentInfo(
            id="codex",
            name="Codex CLI",
            description="OpenAI Codex CLI agent (codex exec).",
            models=_CODEX_MODELS,
            default_model=_CODEX_DEFAULT_MODEL,
            requires_model=False,
        )

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ) -> AsyncIterator[str]:
        async for event in self.stream_reply_events(
            session_id=session_id,
            text=text,
            model_id=model_id,
            workspace_path=workspace_path,
            adapter_state=adapter_state,
        ):
            if event.type == "delta":
                yield event.data.get("delta", "")

    async def stream_reply_events(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        model = model_id or _CODEX_DEFAULT_MODEL
        state = adapter_state or AdapterState()

        if state.native_session_id:
            # RESUME: resume subcommand does NOT support -C flag (verified in T3)
            cmd = [
                "codex",
                "exec",
                "resume",
                state.native_session_id,
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-m",
                model,
                text,
            ]
        else:
            cmd = [
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-m",
                model,
            ]
            if workspace_path:
                cmd.extend(["-C", workspace_path])
            cmd.append(text)

        logger.info(
            "codex: model=%s workspace=%s resume=%s",
            model,
            workspace_path,
            state.native_session_id,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=workspace_path,
        )

        try:
            assert proc.stdout is not None  # noqa: S101
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("codex: non-JSON line: %s", line_str)
                    continue

                etype = event.get("type", "")

                if etype == "thread.started":
                    new_id = event.get("thread_id")
                    if new_id:
                        if (
                            state.native_session_id
                            and new_id != state.native_session_id
                        ):
                            raise RuntimeError("Codex resumed with a different thread")
                        state.native_session_id = new_id

                elif etype == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        yield AgentStreamEvent("delta", {"delta": item.get("text", "")})
                    else:
                        progress = _format_codex_progress_item(item)
                        if progress:
                            yield AgentStreamEvent(
                                "thinking_delta",
                                {
                                    "delta": progress + "\n",
                                    "category": item.get("type") or "item",
                                },
                            )

                elif etype == "error":
                    raise RuntimeError(event.get("message", "codex error"))

                elif etype == "turn.failed":
                    err = event.get("error", {})
                    raise RuntimeError(err.get("message", "codex turn failed"))

        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


# ---------------------------------------------------------------------------
# OpenCode CLI adapter
# ---------------------------------------------------------------------------

_OPENCODE_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        id="amazon-bedrock/anthropic.claude-sonnet-4-6",
        name="Claude Sonnet 4.6 (Bedrock)",
        description="Anthropic Claude Sonnet 4.6 via Amazon Bedrock.",
    ),
    ModelInfo(
        id="amazon-bedrock/anthropic.claude-opus-4-6-v1",
        name="Claude Opus 4.6 (Bedrock)",
        description="Anthropic Claude Opus 4.6 via Amazon Bedrock.",
    ),
    ModelInfo(
        id="openai/gpt-5.4",
        name="GPT-5.4 (OpenAI)",
        description="OpenAI GPT-5.4.",
    ),
)

_OPENCODE_DEFAULT_MODEL = "amazon-bedrock/anthropic.claude-sonnet-4-6"


class OpenCodeAgentAdapter(AgentAdapter):
    @property
    def info(self) -> AgentInfo:
        return AgentInfo(
            id="opencode",
            name="OpenCode",
            description="OpenCode CLI agent (opencode run).",
            models=_OPENCODE_MODELS,
            default_model=_OPENCODE_DEFAULT_MODEL,
            requires_model=True,
        )

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ) -> AsyncIterator[str]:
        model = model_id or _OPENCODE_DEFAULT_MODEL
        state = adapter_state or AdapterState()

        cmd: list[str] = [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            model,
        ]
        if state.native_session_id:
            cmd.extend(["--session", state.native_session_id])
        if workspace_path:
            cmd.extend(["--dir", workspace_path])
        cmd.append(text)

        logger.info(
            "opencode run: model=%s workspace=%s resume=%s",
            model,
            workspace_path,
            state.native_session_id,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )

        try:
            assert proc.stdout is not None  # noqa: S101
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("opencode: non-JSON line: %s", line_str)
                    continue

                etype = event.get("type", "")

                # Capture sessionID from any event (first occurrence)
                sid = event.get("sessionID")
                if sid and not state.native_session_id:
                    state.native_session_id = sid

                if etype == "text":
                    part = event.get("part", {})
                    text_content = part.get("text", "")
                    if text_content:
                        yield text_content

                elif etype == "error":
                    raise RuntimeError(
                        event.get("error", {}).get("message", "opencode error")
                    )

        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


# ---------------------------------------------------------------------------
# Claude Code CLI adapter
# ---------------------------------------------------------------------------

_CLAUDE_CODE_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        id="sonnet",
        name="Claude Sonnet",
        description="Anthropic Claude Sonnet (latest).",
    ),
    ModelInfo(
        id="opus",
        name="Claude Opus",
        description="Anthropic Claude Opus (latest).",
    ),
    ModelInfo(
        id="haiku",
        name="Claude Haiku",
        description="Anthropic Claude Haiku (latest).",
    ),
)

_CLAUDE_CODE_DEFAULT_MODEL = "sonnet"


class ClaudeCodeAgentAdapter(AgentAdapter):
    @property
    def info(self) -> AgentInfo:
        return AgentInfo(
            id="claude-code",
            name="Claude Code",
            description="Anthropic Claude Code CLI agent.",
            models=_CLAUDE_CODE_MODELS,
            default_model=_CLAUDE_CODE_DEFAULT_MODEL,
            requires_model=False,
        )

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ) -> AsyncIterator[str]:
        model = model_id or _CLAUDE_CODE_DEFAULT_MODEL
        state = adapter_state or AdapterState()

        cmd: list[str] = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--dangerously-skip-permissions",
        ]

        if state.native_session_id:
            cmd.extend(["--resume", state.native_session_id])

        cmd.append(text)

        logger.info(
            "claude code: model=%s workspace=%s resume=%s",
            model,
            workspace_path,
            state.native_session_id,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=workspace_path,
        )

        try:
            assert proc.stdout is not None  # noqa: S101
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("claude-code: non-JSON line: %s", line_str[:200])
                    continue

                etype = event.get("type", "")

                if etype == "system":
                    sid = event.get("session_id")
                    if sid and not state.native_session_id:
                        state.native_session_id = sid

                elif etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            yield block.get("text", "")

                elif etype == "result":
                    if event.get("is_error"):
                        raise RuntimeError(event.get("result", "claude code error"))
                    sid = event.get("session_id")
                    if sid and not state.native_session_id:
                        state.native_session_id = sid
                    # Do NOT yield result.result — it duplicates assistant text

        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


# ---------------------------------------------------------------------------
# GitHub Copilot CLI adapter
# ---------------------------------------------------------------------------

_COPILOT_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        id="gpt-5.2",
        name="GPT-5.2",
        description="OpenAI GPT-5.2 via GitHub Copilot.",
    ),
    ModelInfo(
        id="claude-sonnet-4-5",
        name="Claude Sonnet 4.5",
        description="Anthropic Claude Sonnet 4.5 via GitHub Copilot.",
    ),
)

_COPILOT_DEFAULT_MODEL = "gpt-5.2"


class CopilotAgentAdapter(AgentAdapter):
    @property
    def info(self) -> AgentInfo:
        return AgentInfo(
            id="copilot",
            name="GitHub Copilot",
            description="GitHub Copilot CLI agent.",
            models=_COPILOT_MODELS,
            default_model=_COPILOT_DEFAULT_MODEL,
            requires_model=False,
        )

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        model_id: str | None,
        workspace_path: str | None = None,
        adapter_state: AdapterState | None = None,
    ) -> AsyncIterator[str]:
        model = model_id or _COPILOT_DEFAULT_MODEL
        state = adapter_state or AdapterState()

        cmd: list[str] = [
            "copilot",
            "-p",
            text,
            "--output-format",
            "json",
            "--allow-all-tools",
            "-s",
            "--model",
            model,
            "--no-custom-instructions",
        ]

        if state.native_session_id:
            cmd.append(f"--resume={state.native_session_id}")

        logger.info(
            "copilot: model=%s workspace=%s resume=%s",
            model,
            workspace_path,
            state.native_session_id,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=workspace_path,
        )

        try:
            assert proc.stdout is not None  # noqa: S101
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("copilot: non-JSON line: %s", line_str[:200])
                    continue

                etype = event.get("type", "")

                if etype == "assistant.message":
                    content = event.get("data", {}).get("content", "")
                    if content:
                        yield content

                elif etype == "result":
                    sid = event.get("sessionId")
                    if sid and not state.native_session_id:
                        state.native_session_id = sid
                    if event.get("exitCode", 0) != 0:
                        raise RuntimeError("copilot exited with error")

        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AgentRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        self._adapters[adapter.info.id] = adapter

    def list_agents(self) -> list[AgentInfo]:
        return [adapter.info for adapter in self._adapters.values()]

    def get(self, agent_id: str) -> AgentAdapter:
        try:
            return self._adapters[agent_id]
        except KeyError as exc:
            raise KeyError(f"Unknown agent: {agent_id}") from exc

    def resolve_model(self, agent_id: str, requested_model: str | None) -> str | None:
        adapter = self.get(agent_id)
        info = adapter.info
        model_ids = {model.id for model in info.models}
        if requested_model:
            if model_ids and requested_model not in model_ids:
                raise KeyError(f"Unknown model for {agent_id}: {requested_model}")
            return requested_model
        if info.default_model:
            return info.default_model
        if info.requires_model:
            raise ValueError(f"Model is required for agent: {agent_id}")
        return None


registry = AgentRegistry()
registry.register(EchoAgentAdapter())
if shutil.which("codex"):
    registry.register(CodexAgentAdapter())
if shutil.which("opencode"):
    registry.register(OpenCodeAgentAdapter())
if shutil.which("claude"):
    registry.register(ClaudeCodeAgentAdapter())
if shutil.which("copilot"):
    registry.register(CopilotAgentAdapter())
