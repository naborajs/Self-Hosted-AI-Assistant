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

    # Phase 3: Adaptive Model Selection & Concurrency Configuration
    ollama_model_cache_ttl: float = Field(60.0, env="OLLAMA_MODEL_CACHE_TTL")
    ollama_max_concurrency: int = Field(3, env="OLLAMA_MAX_CONCURRENCY")
    allow_user_model_selection: bool = Field(False, env="ALLOW_USER_MODEL_SELECTION")

    # Phase 4B: Controlled Tool Execution Configuration
    enable_tool_execution: bool = Field(True, env="ENABLE_TOOL_EXECUTION")
    tool_max_rounds: int = Field(3, env="TOOL_MAX_ROUNDS")
    tool_max_calls_per_request: int = Field(5, env="TOOL_MAX_CALLS_PER_REQUEST")
    tool_timeout_seconds: float = Field(15.0, env="TOOL_TIMEOUT_SECONDS")
    tool_max_output_chars: int = Field(2000, env="TOOL_MAX_OUTPUT_CHARS")
    allow_external_network_tools: bool = Field(False, env="ALLOW_EXTERNAL_NETWORK_TOOLS")
    tool_workspace_root: Path = Field(default_factory=lambda: Path.cwd() / "storage" / "workspace")
    tool_default_permission_policy: str = Field("standard", env="TOOL_DEFAULT_PERMISSION_POLICY")

    # Phase 5A: Integration & Network Security Configuration
    allow_remote_api: bool = Field(False, env="ALLOW_REMOTE_API")

    # Phase 5B: Machine-Facing API Keys (comma-separated 'key' or 'client_id:key')
    api_keys: Optional[SecretStr] = Field(None, env="API_KEYS")

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

    @property
    def is_loopback_only(self) -> bool:
        host = self.app_host.lower().strip()
        return host in ("127.0.0.1", "localhost", "::1")

    @property
    def valid_api_keys(self) -> list[str]:
        if not self.api_keys:
            return []
        raw = self.api_keys.get_secret_value() if hasattr(self.api_keys, "get_secret_value") else str(self.api_keys)
        return [k.strip() for k in raw.split(",") if k.strip()]


settings = Settings()
