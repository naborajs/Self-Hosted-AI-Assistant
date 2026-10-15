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
    admin_password_hash: Optional[SecretStr] = Field(None, env="ADMIN_PASSWORD_HASH")
    rate_limit_per_minute: int = Field(60, env="RATE_LIMIT_PER_MINUTE")

    whatsapp_bridge_url: AnyHttpUrl = Field("http://127.0.0.1:3000", env="WHATSAPP_BRIDGE_URL")
    whatsapp_bridge_host: str = Field("127.0.0.1", env="WHATSAPP_BRIDGE_HOST")
    whatsapp_bridge_port: int = Field(3000, env="WHATSAPP_BRIDGE_PORT")
    whatsapp_bridge_storage: Path = Field(default_factory=lambda: Path.cwd() / "storage" / "whatsapp_bridge")
    whatsapp_bridge_secret: Optional[SecretStr] = Field(None, env="WHATSAPP_BRIDGE_SECRET")

    storage_path: Path = Field(default_factory=lambda: Path.cwd() / "storage")
    encrypted_sessions_path: Path = Field(default_factory=lambda: Path.cwd() / "storage" / "sessions")
    cors_origins: str = Field(
        "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:3000,http://localhost:3000",
        env="CORS_ORIGINS",
    )

    # Phase 2: Inbound Inbox & Worker Pool Configuration
    inbox_worker_count: int = Field(3, env="INBOX_WORKER_COUNT")
    inbox_max_batch_size: int = Field(50, env="INBOX_MAX_BATCH_SIZE")
    inbox_max_attempts: int = Field(3, env="INBOX_MAX_ATTEMPTS")
    inbox_lease_timeout: float = Field(120.0, env="INBOX_LEASE_TIMEOUT")
    inbox_poll_interval: float = Field(1.0, env="INBOX_POLL_INTERVAL")
    inbox_max_pending: int = Field(200, env="INBOX_MAX_PENDING")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @property
    def admin_list(self) -> list[str]:
        if not self.admin_users:
            return []
        return [item.strip() for item in self.admin_users.split(",") if item.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.cors_origins:
            return []
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
