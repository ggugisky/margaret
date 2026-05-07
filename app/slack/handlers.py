from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from app.adapters import AdapterState, AgentRegistry
from app.store import Store

if TYPE_CHECKING:
    from app.rag_memory import RagMemory

from .models import SlackCommand, SlackMessageContext

logger = logging.getLogger(__name__)


class SlackDMHandler:
    def __init__(
        self,
        *,
        store: Store,
        registry: AgentRegistry,
        default_agent: str,
        workspace_root: str = "/workspace",
        rag_memory: RagMemory | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._default_agent = default_agent
        self._workspace_root = workspace_root
        self._rag = rag_memory

    async def handle_message(
        self,
        *,
        event: dict,
        team_id: str,
        say,
        client: Any | None = None,
        set_status: Any | None = None,
    ) -> None:
        event_type = event.get("type")
        is_dm_message = event_type == "message" and event.get("channel_type") == "im"
        is_app_mention = event_type == "app_mention"
        is_channel_message = (
            event_type == "message" and event.get("channel_type") != "im"
        )

        if not (is_dm_message or is_app_mention or is_channel_message):
            logger.info(
                "slack: ignoring event type=%s channel_type=%s subtype=%s",
                event_type,
                event.get("channel_type"),
                event.get("subtype"),
            )
            return
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        user_id = event.get("user")
        channel_id = event.get("channel")
        text = (event.get("text") or "").strip()
        if is_app_mention:
            text = self._strip_leading_mention(text)
        message_ts = event.get("ts")
        if not user_id or not channel_id or not message_ts or not text:
            return

        raw_thread_ts = event.get("thread_ts")

        if is_channel_message and not is_app_mention:
            if not raw_thread_ts:
                return
            existing = self._store.get_slack_thread_by_ts(
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=raw_thread_ts,
            )
            if not existing:
                return
            if existing.get("owner_user_id") != user_id:
                return

        ctx = SlackMessageContext(
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            text=text,
            thread_ts=raw_thread_ts or message_ts,
            message_ts=message_ts,
            is_dm=is_dm_message,
            username=await self._resolve_username(user_id=user_id, client=client),
        )

        existing_session_id = self._store.get_session_id_by_slack_thread(
            team_id=ctx.team_id,
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            user_id=ctx.user_id,
        )

        if existing_session_id is None and not ctx.is_dm and raw_thread_ts:
            thread_row = self._store.get_slack_thread_by_ts(
                team_id=ctx.team_id,
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
            )
            if thread_row and thread_row.get("owner_user_id") == ctx.user_id:
                existing_session_id = str(thread_row["session_id"])

        is_new_thread = existing_session_id is None

        command = self._parse_command(text=ctx.text)

        if command and command.kind == "agents":
            await say(
                text=self._format_agents_message(),
                thread_ts=ctx.thread_ts,
            )
            return

        if not is_new_thread and command is not None:
            session = self._store.get_session(existing_session_id)
            agent_id = str(session["agent_id"]) if session else "unknown"
            model_id = (
                str(session.get("model_id") or "default") if session else "default"
            )
            await say(
                text=(
                    f"This thread is locked to `{agent_id}` / `{model_id}`. "
                    "Start a new DM thread to switch agent/model."
                ),
                thread_ts=ctx.thread_ts,
            )
            return

        if command and command.kind == "default":
            assert command.agent_id is not None  # noqa: S101
            assert command.model_id is not None  # noqa: S101
            model_id = self._resolve_model_or_reply(
                agent_id=command.agent_id,
                model_id=command.model_id,
            )
            if model_id is None:
                await say(
                    text=self._invalid_agent_model_message(
                        agent_id=command.agent_id,
                        model_id=command.model_id,
                    ),
                    thread_ts=ctx.thread_ts,
                )
                return
            self._store.upsert_slack_user_default(
                team_id=ctx.team_id,
                user_id=ctx.user_id,
                agent_id=command.agent_id,
                model_id=model_id,
            )
            await say(
                text=(
                    f"Saved default for new threads: `{command.agent_id}` / `{model_id}`."
                ),
                thread_ts=ctx.thread_ts,
            )
            return

        if not is_new_thread:
            assert existing_session_id is not None  # noqa: S101
            await self._reply_with_agent_turn(
                session_id=existing_session_id,
                text=ctx.text,
                ctx=ctx,
                say=say,
                client=client,
                set_status=set_status,
            )
            return

        session_id: str
        first_turn_text: str | None

        if command and command.kind == "start":
            assert command.agent_id is not None  # noqa: S101
            assert command.model_id is not None  # noqa: S101
            model_id = self._resolve_model_or_reply(
                agent_id=command.agent_id,
                model_id=command.model_id,
            )
            if model_id is None:
                await say(
                    text=self._invalid_agent_model_message(
                        agent_id=command.agent_id,
                        model_id=command.model_id,
                    ),
                    thread_ts=ctx.thread_ts,
                )
                return
            session_id = self._create_slack_session(
                ctx=ctx,
                agent_id=command.agent_id,
                model_id=model_id,
                is_mention=is_app_mention,
            )
            first_turn_text = (command.prompt or "").strip() or None
            if not first_turn_text:
                await say(
                    text=(
                        f"Started new thread with `{command.agent_id}` / `{model_id}`."
                    ),
                    thread_ts=ctx.thread_ts,
                )
                return
        else:
            default_pref = self._store.get_slack_user_default(
                team_id=ctx.team_id,
                user_id=ctx.user_id,
            )
            if default_pref:
                agent_id = str(default_pref["agent_id"])
                requested_model = str(default_pref["model_id"])
            else:
                agent_id = self._default_agent
                requested_model = None

            model_id = self._resolve_model_or_reply(
                agent_id=agent_id,
                model_id=requested_model,
            )
            if model_id is None:
                await say(
                    text=self._invalid_agent_model_message(
                        agent_id=agent_id,
                        model_id=requested_model,
                    ),
                    thread_ts=ctx.thread_ts,
                )
                return
            session_id = self._create_slack_session(
                ctx=ctx,
                agent_id=agent_id,
                model_id=model_id,
                is_mention=is_app_mention,
            )
            first_turn_text = ctx.text

        await self._reply_with_agent_turn(
            session_id=session_id,
            text=first_turn_text,
            ctx=ctx,
            say=say,
            client=client,
            set_status=set_status,
        )

    def _create_slack_session(
        self,
        *,
        ctx: SlackMessageContext,
        agent_id: str,
        model_id: str,
        is_mention: bool = False,
    ) -> str:
        workspace_path = os.path.join(self._workspace_root, ctx.username or ctx.user_id)
        os.makedirs(workspace_path, exist_ok=True)
        session = self._store.create_session(
            agent_id=agent_id,
            model_id=model_id,
            title="Slack session",
            client="slack",
            workspace_path=workspace_path,
        )
        session_id = str(session["session_id"])
        self._store.upsert_slack_thread_mapping(
            team_id=ctx.team_id,
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            user_id=ctx.user_id,
            session_id=session_id,
            owner_user_id=ctx.user_id if is_mention else None,
        )
        return session_id

    async def _resolve_username(self, *, user_id: str, client: Any | None) -> str:
        if not client:
            return user_id
        try:
            result = await client.users_info(user=user_id)
            name = (
                result.get("user", {}).get("name")
                or result.get("user", {}).get("profile", {}).get("display_name")
                or ""
            )
            clean = "".join(c for c in name if c.isalnum() or c in "-_.")
            return clean or user_id
        except Exception:
            logger.warning("slack: failed to resolve username for %s", user_id)
            return user_id

    def _format_agents_message(self) -> str:
        lines: list[str] = ["*Available Agents*\n"]
        for agent in self._registry.list_agents():
            default = (
                f" (default: `{agent.default_model}`)" if agent.default_model else ""
            )
            required = " ⚠️ model required" if agent.requires_model else ""
            lines.append(f"• *{agent.id}*{required}{default}")
            for model in agent.models:
                lines.append(
                    f"  ‣ `{model.id}` — {model.description}"
                    if model.description
                    else f"  ‣ `{model.id}`"
                )
        lines.append(
            "\n_Usage: `@bot <agent> <model> [prompt]` or `@bot default <agent> <model>`_"
        )
        return "\n".join(lines)

    def _parse_command(self, *, text: str) -> SlackCommand | None:
        tokens = text.split()
        if not tokens:
            return None

        start = 1 if tokens[0].startswith("<@") and tokens[0].endswith(">") else 0
        if start >= len(tokens):
            return None
        core = tokens[start:]
        if not core:
            return None

        head = core[0]
        if head == "agents":
            return SlackCommand(kind="agents")

        if head == "default":
            if len(core) != 3:
                return None
            return SlackCommand(kind="default", agent_id=core[1], model_id=core[2])

        if len(core) < 2:
            return None
        if not self._is_known_agent(core[0]):
            return None

        prompt = " ".join(core[2:]).strip() if len(core) > 2 else None
        return SlackCommand(
            kind="start",
            agent_id=core[0],
            model_id=core[1],
            prompt=prompt,
        )

    def _strip_leading_mention(self, text: str) -> str:
        tokens = text.split(maxsplit=1)
        if tokens and tokens[0].startswith("<@") and tokens[0].endswith(">"):
            return tokens[1].strip() if len(tokens) > 1 else ""
        return text

    def _is_known_agent(self, agent_id: str) -> bool:
        return any(agent.id == agent_id for agent in self._registry.list_agents())

    def _resolve_model_or_reply(
        self, *, agent_id: str, model_id: str | None
    ) -> str | None:
        try:
            return self._registry.resolve_model(agent_id, model_id)
        except (KeyError, ValueError):
            return None

    def _invalid_agent_model_message(
        self, *, agent_id: str, model_id: str | None
    ) -> str:
        agents = self._registry.list_agents()
        known_agents = ", ".join(sorted(agent.id for agent in agents))
        model_hint = f"`{model_id}`" if model_id is not None else "the default model"
        return (
            f"Invalid agent/model: `{agent_id}` / {model_hint}. "
            f"Known agents: {known_agents}. "
            "Use `/agents` to see available models for each agent."
        )

    async def _reply_with_agent_turn(
        self,
        *,
        session_id: str,
        text: str,
        ctx: SlackMessageContext,
        say,
        client: Any | None,
        set_status: Any | None = None,
    ) -> None:
        session = self._store.get_session(session_id)
        agent_id = str(session["agent_id"]) if session else "?"
        model_id = str(session.get("model_id") or "default") if session else "?"
        label = f"`{agent_id}` / `{model_id}`"

        if client is None:
            reply_text = await self._run_agent_turn(session_id=session_id, text=text)
            await say(text=reply_text, thread_ts=ctx.thread_ts)
            return

        responder = SlackStreamingResponder(
            client=client,
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            set_status=set_status,
            label=label,
            user_id=ctx.user_id,
            team_id=ctx.team_id,
        )
        await responder.start()

        stop_status = asyncio.Event()
        status_task = asyncio.create_task(responder.keep_status_alive(stop_status))

        try:
            reply_text = await self._run_agent_turn(
                session_id=session_id,
                text=text,
                on_delta=responder.append,
            )
            if reply_text.startswith("[margaret error]"):
                await responder.error(reply_text)
            else:
                await responder.finish(reply_text)
        finally:
            stop_status.set()
            await status_task

    async def _run_agent_turn(
        self,
        *,
        session_id: str,
        text: str,
        on_delta: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> str:
        session = self._store.get_session(session_id)
        if not session:
            raise RuntimeError(f"Unknown session: {session_id}")

        self._store.append_event(session_id=session_id, role="user", content=text)
        if self._rag:
            asyncio.create_task(self._rag.index_event(session_id, "user", text))
        self._store.set_session_status(session_id, "running")

        agent_text = text
        if self._rag:
            mem_ctx = await self._rag.build_context(text)
            if mem_ctx:
                agent_text = mem_ctx + text

        agent = self._registry.get(str(session["agent_id"]))
        binding = self._store.get_adapter_binding(session_id)
        state = AdapterState()
        if binding and binding.get("adapter_state_json"):
            try:
                state_data = json.loads(str(binding["adapter_state_json"]))
                state.native_session_id = state_data.get("native_session_id")
            except Exception:
                logger.warning("failed to parse adapter state for %s", session_id)

        last_persisted_id = state.native_session_id
        chunks: list[str] = []

        try:
            async for delta in self._stream_agent(
                agent=agent,
                session=session,
                text=agent_text,
                state=state,
            ):
                if state.native_session_id != last_persisted_id:
                    last_persisted_id = state.native_session_id
                    self._persist_adapter_state(
                        session_id=session_id, session=session, state=state
                    )
                chunks.append(delta)
                if on_delta is not None:
                    await on_delta(delta, "".join(chunks))

            if state.native_session_id and state.native_session_id != last_persisted_id:
                self._persist_adapter_state(
                    session_id=session_id, session=session, state=state
                )

            final_text = "".join(chunks).strip()
            self._store.append_event(
                session_id=session_id,
                role="assistant",
                content=final_text,
            )
            if self._rag and final_text:
                asyncio.create_task(
                    self._rag.index_event(session_id, "assistant", final_text)
                )
            return final_text or "(no response)"
        except Exception as exc:
            self._store.append_event(
                session_id=session_id, role="error", content=str(exc)
            )
            return f"[margaret error] {exc}"
        finally:
            self._store.set_session_status(session_id, "idle")

    async def _stream_agent(
        self,
        *,
        agent,
        session: dict,
        text: str,
        state: AdapterState,
    ) -> AsyncIterator[str]:
        async for delta in agent.stream_reply(
            session_id=str(session["session_id"]),
            text=text,
            model_id=session.get("model_id"),
            workspace_path=session.get("workspace_path"),
            adapter_state=state,
        ):
            yield delta

    def _persist_adapter_state(
        self,
        *,
        session_id: str,
        session: dict,
        state: AdapterState,
    ) -> None:
        self._store.upsert_adapter_binding(
            session_id=session_id,
            adapter_name=str(session["agent_id"]),
            adapter_state_json=json.dumps(
                {
                    "native_session_id": state.native_session_id,
                }
            ),
            workspace_path=session.get("workspace_path"),
        )


class SlackStreamingResponder:
    def __init__(
        self,
        *,
        client: Any,
        channel_id: str,
        thread_ts: str,
        throttle_seconds: float = 0.3,
        set_status: Any | None = None,
        label: str = "",
        user_id: str = "",
        team_id: str = "",
    ) -> None:
        self._client = client
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self._throttle_seconds = throttle_seconds
        self._set_status = set_status
        self._label = label
        self._user_id = user_id
        self._team_id = team_id
        self._stream_ts: str | None = None
        self._fallback_ts: str | None = None
        self._pending_delta = ""
        self._last_update = 0.0
        self._delta_received = False
        self._started_at = 0.0

    def _status_text(self) -> str:
        elapsed = int(time.monotonic() - self._started_at) if self._started_at else 0
        base = f"⏳ {self._label} is thinking" if self._label else "⏳ thinking"
        return f"{base} ({elapsed}s)" if elapsed else base

    async def start(self) -> None:
        self._started_at = time.monotonic()
        initial = self._status_text()
        if self._set_status is not None:
            try:
                await self._set_status(initial)
            except Exception:
                logger.debug("slack set_status failed", exc_info=True)

        try:
            kwargs: dict = {
                "channel": self._channel_id,
                "thread_ts": self._thread_ts,
            }
            if self._user_id:
                kwargs["recipient_user_id"] = self._user_id
            if self._team_id:
                kwargs["recipient_team_id"] = self._team_id
            response = await self._client.chat_startStream(**kwargs)
            stream_ts = response.get("ts") if isinstance(response, dict) else None
            if stream_ts:
                self._stream_ts = str(stream_ts)
                return
        except Exception:
            logger.debug("slack startStream failed, using fallback", exc_info=True)

        response = await self._client.chat_postMessage(
            channel=self._channel_id,
            text=initial,
            thread_ts=self._thread_ts,
        )
        message = response.get("message", {}) if isinstance(response, dict) else {}
        self._fallback_ts = str(message.get("ts") or response.get("ts") or "")

    async def _clear_status(self) -> None:
        if self._set_status is not None:
            try:
                await self._set_status("")
            except Exception:
                logger.debug("slack clear set_status failed", exc_info=True)

    async def keep_status_alive(self, stop_event: asyncio.Event) -> None:
        interval = 2.5
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()), timeout=interval
                )
                return
            except asyncio.TimeoutError:
                pass
            if self._delta_received:
                continue
            status = self._status_text()
            if self._set_status is not None:
                try:
                    await self._set_status(status)
                except Exception:
                    pass
            if self._fallback_ts:
                try:
                    await self._client.chat_update(
                        channel=self._channel_id,
                        ts=self._fallback_ts,
                        text=status,
                    )
                except Exception:
                    pass

    async def append(self, delta: str, full_text: str) -> None:
        if not delta:
            return

        if not self._delta_received:
            self._delta_received = True
            delta = delta.lstrip("\n")
            if not delta:
                return

        if self._stream_ts:
            self._pending_delta += delta
            now = time.monotonic()
            if now - self._last_update >= self._throttle_seconds:
                await self._flush_stream()
            return

        now = time.monotonic()
        if now - self._last_update < self._throttle_seconds:
            return
        await self._fallback_update(f"{self._format_text(full_text.lstrip())}\n\n_..._")
        self._last_update = now

    async def finish(self, text: str) -> None:
        final_text = self._format_text(text.strip() or "(no response)")
        if self._stream_ts:
            stop_kwargs: dict = {
                "channel": self._channel_id,
                "ts": self._stream_ts,
            }
            if self._pending_delta:
                stop_kwargs["markdown_text"] = self._pending_delta
                self._pending_delta = ""
            try:
                await self._client.chat_stopStream(**stop_kwargs)
                await self._clear_status()
                return
            except Exception:
                logger.exception("slack stopStream failed, using fallback")
        await self._fallback_update(final_text)
        await self._clear_status()

    async def error(self, text: str) -> None:
        if self._stream_ts:
            try:
                await self._client.chat_stopStream(
                    channel=self._channel_id,
                    ts=self._stream_ts,
                    markdown_text=f":x: {text}",
                )
                await self._clear_status()
                return
            except Exception:
                logger.exception("slack stopStream error failed, using fallback")
        await self._fallback_update(f":x: {text}")
        await self._clear_status()

    async def _flush_stream(self, *, force: bool = False) -> None:
        if not self._stream_ts or not self._pending_delta:
            return
        now = time.monotonic()
        if not force and now - self._last_update < self._throttle_seconds:
            return
        try:
            await self._client.chat_appendStream(
                channel=self._channel_id,
                ts=self._stream_ts,
                markdown_text=self._pending_delta,
            )
            self._pending_delta = ""
            self._last_update = now
        except Exception:
            logger.exception("slack appendStream failed")

    async def _fallback_update(self, text: str) -> None:
        if not self._fallback_ts:
            return
        try:
            await self._client.chat_update(
                channel=self._channel_id,
                ts=self._fallback_ts,
                text=text,
            )
        except Exception:
            logger.exception("slack fallback update failed")

    def _format_text(self, text: str) -> str:
        return text.replace("**", "*")
