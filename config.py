from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr, AnyHttpUrl
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_host: str = Field("127.0.0.1", env="APP_HOST")
    app_port: int = Field(8000, env="APP_PORT")
    log_level: str = Field("INFO", env="LOG_LEVEL")

    whatsapp_phone: str = Field(..., env="WHATSAPP_PHONE")
    telegram_bot_token: str = Field(..., env="TELEGRAM_BOT_TOKEN")
    ollama_model: str = Field("qwen3:8b", env="OLLAMA_MODEL")
    ollama_url: AnyHttpUrl = Field("http://127.0.0.1:11434", env="OLLAMA_URL")

    database_url: str = Field("sqlite+aiosqlite:///./assistant.db", env="DATABASE_URL")
    session_secret: SecretStr = Field(..., env="SESSION_SECRET")
    admin_users: str = Field("", env="ADMIN_USERS")
    rate_limit_per_minute: int = Field(60, env="RATE_LIMIT_PER_MINUTE")

    whatsapp_bridge_url: AnyHttpUrl = Field("http://127.0.0.1:3000", env="WHATSAPP_BRIDGE_URL")
    whatsapp_bridge_host: str = Field("127.0.0.1", env="WHATSAPP_BRIDGE_HOST")
    whatsapp_bridge_port: int = Field(3000, env="WHATSAPP_BRIDGE_PORT")
    whatsapp_bridge_storage: Path = Field(default_factory=lambda: Path.cwd() / "storage" / "whatsapp_bridge")

    storage_path: Path = Field(default_factory=lambda: Path.cwd() / "storage")
    encrypted_sessions_path: Path = Field(default_factory=lambda: Path.cwd() / "storage" / "sessions")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @property
    def admin_list(self) -> list[str]:
        if not self.admin_users:
            return []
        return [item.strip() for item in self.admin_users.split(",") if item.strip()]


settings = Settings()
