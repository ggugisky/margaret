from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

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
        rag_memory: RagMemory | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._default_agent = default_agent
        self._rag = rag_memory

    async def handle_message(
        self,
        *,
        event: dict,
        team_id: str,
        say,
    ) -> None:
        if event.get("type") != "message":
            return
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        user_id = event.get("user")
        channel_id = event.get("channel")
        text = (event.get("text") or "").strip()
        message_ts = event.get("ts")
        if not user_id or not channel_id or not message_ts or not text:
            return

        ctx = SlackMessageContext(
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            text=text,
            thread_ts=event.get("thread_ts") or message_ts,
            message_ts=message_ts,
        )

        existing_session_id = self._store.get_session_id_by_slack_thread(
            team_id=ctx.team_id,
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            user_id=ctx.user_id,
        )
        is_new_thread = existing_session_id is None

        command = self._parse_command(text=ctx.text)

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
            reply_text = await self._run_agent_turn(
                session_id=existing_session_id, text=ctx.text
            )
            await say(text=reply_text, thread_ts=ctx.thread_ts)
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
            )
            first_turn_text = ctx.text

        reply_text = await self._run_agent_turn(
            session_id=session_id, text=first_turn_text
        )
        await say(text=reply_text, thread_ts=ctx.thread_ts)

    def _create_slack_session(
        self,
        *,
        ctx: SlackMessageContext,
        agent_id: str,
        model_id: str,
    ) -> str:
        session = self._store.create_session(
            agent_id=agent_id,
            model_id=model_id,
            title="Slack DM session",
            client="slack",
            workspace_path=None,
        )
        session_id = str(session["session_id"])
        self._store.upsert_slack_thread_mapping(
            team_id=ctx.team_id,
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            user_id=ctx.user_id,
            session_id=session_id,
        )
        return session_id

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

    async def _run_agent_turn(self, *, session_id: str, text: str) -> str:
        session = self._store.get_session(session_id)
        if not session:
            raise RuntimeError(f"Unknown session: {session_id}")

        self._store.append_event(session_id=session_id, role="user", content=text)
        if self._rag:
            asyncio.create_task(self._rag.index_event(session_id, "user", text))
        self._store.set_session_status(session_id, "running")

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
                text=text,
                state=state,
            ):
                if state.native_session_id != last_persisted_id:
                    last_persisted_id = state.native_session_id
                    self._persist_adapter_state(
                        session_id=session_id, session=session, state=state
                    )
                chunks.append(delta)

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
