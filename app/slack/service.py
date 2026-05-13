from __future__ import annotations

import logging

from slack_bolt.async_app import AsyncApp  # pyright: ignore[reportMissingImports]
from slack_bolt.adapter.socket_mode.aiohttp import (  # pyright: ignore[reportMissingImports]
    AsyncSocketModeHandler,
)
from slack_bolt.middleware.assistant.async_assistant import (  # pyright: ignore[reportMissingImports]
    AsyncAssistant,
)

from app.config import Settings

from .handlers import SlackDMHandler

logger = logging.getLogger(__name__)


class SlackIntegration:
    def __init__(self, *, settings: Settings, handler: SlackDMHandler) -> None:
        self._settings = settings
        self._handler_logic = handler
        self._app: AsyncApp | None = None
        self._socket_handler: AsyncSocketModeHandler | None = None
        self._connected = False

    @property
    def enabled(self) -> bool:
        return bool(self._settings.slack_enabled)

    @property
    def running(self) -> bool:
        return self._connected

    def can_start(self) -> bool:
        if not self.enabled:
            return False
        return bool(self._settings.slack_app_token and self._settings.slack_bot_token)

    async def connect(self) -> bool:
        if self._connected:
            return True
        if not self.can_start():
            logger.info("slack integration disabled or missing tokens")
            return False

        app = AsyncApp(token=self._settings.slack_bot_token)
        assistant = AsyncAssistant()

        @assistant.thread_started
        async def _on_assistant_thread_started(say, set_suggested_prompts):  # noqa: ANN001
            await set_suggested_prompts(
                prompts=[
                    {
                        "title": "Ask Margaret",
                        "message": "What can you help me with?",
                    },
                    {
                        "title": "Use Codex",
                        "message": "codex gpt-5.5 help me inspect this project",
                    },
                ],
            )
            await say("무엇을 도와드릴까요?")

        @assistant.user_message
        async def _on_assistant_user_message(event, say, body, client, set_status):  # noqa: ANN001
            team_id = (body.get("team_id") if isinstance(body, dict) else None) or ""
            await self._handler_logic.handle_message(
                event=event,
                team_id=team_id,
                say=say,
                client=client,
                set_status=set_status,
            )

        app.assistant(assistant)

        @app.event("message")
        async def _on_message(event, say, body, logger, client):  # noqa: ANN001
            team_id = (body.get("team_id") if isinstance(body, dict) else None) or ""
            await self._handler_logic.handle_message(
                event=event,
                team_id=team_id,
                say=say,
                client=client,
            )

        @app.event("app_mention")
        async def _on_app_mention(event, say, body, logger, client):  # noqa: ANN001
            team_id = (body.get("team_id") if isinstance(body, dict) else None) or ""
            await self._handler_logic.handle_message(
                event=event,
                team_id=team_id,
                say=say,
                client=client,
            )

        self._app = app
        self._socket_handler = AsyncSocketModeHandler(
            app, self._settings.slack_app_token
        )
        socket_handler = self._socket_handler
        assert socket_handler is not None  # noqa: S101
        await socket_handler.connect_async()
        self._connected = True
        logger.info("slack socket mode connected")
        return True

    async def close(self) -> None:
        if not self._socket_handler:
            self._connected = False
            return
        await self._socket_handler.close_async()
        self._connected = False
        logger.info("slack socket mode closed")
