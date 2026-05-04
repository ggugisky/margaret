from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


class Settings:
    port: int = int(os.getenv("PORT", "8787"))
    database_path: str = os.getenv(
        "MARGARET_DB_PATH",
        str(Path.home() / ".margaret" / "gateway.sqlite3"),
    )
    gateway_token: str = os.getenv("MARGARET_GATEWAY_TOKEN", "")
    default_agent: str = os.getenv("MARGARET_DEFAULT_AGENT", "echo")


settings = Settings()

