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
        self.voice_app_secret: str = os.getenv("VOICE_APP_SECRET", "")
        self.voice_jwt_secret: str = os.getenv(
            "VOICE_JWT_SECRET",
            self.gateway_token or self.voice_app_secret,
        )
        self.voice_msg_hmac_key: str = os.getenv("VOICE_MSG_HMAC_KEY", "")
        self.voice_workspace_root: str = os.getenv(
            "VOICE_WORKSPACE_ROOT", "/workspace"
        )
        self.voice_workspace_name: str = os.getenv("VOICE_WORKSPACE_NAME", "")
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
        self.elevenlabs_voice_id: str = os.getenv(
            "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
        )
        self.default_tts_provider: str = os.getenv("TTS_PROVIDER", "openai-hd")
        self.default_agent: str = os.getenv("MARGARET_DEFAULT_AGENT", "echo")
        self.slack_app_token: str = os.getenv("SLACK_APP_TOKEN", "")
        self.slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN", "")
        self.slack_enabled: bool = os.getenv("SLACK_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.rag_enabled: bool = os.getenv("MARGARET_RAG_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.rag_working_dir: str = os.getenv(
            "MARGARET_RAG_DIR",
            str(Path.home() / ".margaret" / "rag"),
        )


settings = Settings()
