from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


class Settings:
    def __init__(self) -> None:
        self.port: int = int(os.getenv("PORT", "8787"))
        self.database_path: str = os.getenv(
            "MARGARET_DB_PATH",
            str(Path.home() / ".margaret" / "gateway.sqlite3"),
        )
        self.gateway_token: str = os.getenv("MARGARET_GATEWAY_TOKEN", "")
        self.default_agent: str = os.getenv("MARGARET_DEFAULT_AGENT", "echo")
        self.slack_app_token: str = os.getenv("SLACK_APP_TOKEN", "")
        self.slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN", "")
        self.slack_enabled: bool = os.getenv("SLACK_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }


settings = Settings()
