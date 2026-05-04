from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from app.adapters import AdapterState, AgentRegistry
from app.store import Store

from .models import SlackMessageContext

logger = logging.getLogger(__name__)


class SlackDMHandler:
    def __init__(
        self,
        *,
        store: Store,
        registry: AgentRegistry,
        default_agent: str,
    ) -> None:
        self._store = store
        self._registry = registry
        self._default_agent = default_agent

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

        session_id = self._resolve_or_create_session(ctx)
        reply_text = await self._run_agent_turn(session_id=session_id, text=ctx.text)
        await say(text=reply_text, thread_ts=ctx.thread_ts)

    def _resolve_or_create_session(self, ctx: SlackMessageContext) -> str:
        session_id = self._store.get_session_id_by_slack_thread(
            team_id=ctx.team_id,
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            user_id=ctx.user_id,
        )
        if session_id:
            return session_id

        model_id = self._registry.resolve_model(self._default_agent, None)
        session = self._store.create_session(
            agent_id=self._default_agent,
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

    async def _run_agent_turn(self, *, session_id: str, text: str) -> str:
        session = self._store.get_session(session_id)
        if not session:
            raise RuntimeError(f"Unknown session: {session_id}")

        self._store.append_event(session_id=session_id, role="user", content=text)
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
