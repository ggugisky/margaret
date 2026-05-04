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
