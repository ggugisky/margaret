from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlackMessageContext:
    team_id: str
    channel_id: str
    user_id: str
    text: str
    thread_ts: str
    message_ts: str
    is_dm: bool = False
    username: str = ""


@dataclass(frozen=True)
class SlackCommand:
    kind: str
    agent_id: str | None = None
    model_id: str | None = None
    prompt: str | None = None
