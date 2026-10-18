"""Project EDITH v2.0 — Central Application Configuration & Environment Management.

This module defines the unified Pydantic settings schema governing all Project EDITH
subsystems. It handles:
1. Environment Variable Parsing: Reads configuration from the local `.env` file and
   system environment variables with strict type coercion and validation.
2. Secret Isolation: Uses `pydantic.SecretStr` for sensitive credentials (session signing
   keys, administrative password hashes, WhatsApp bridge secrets, and machine API keys)
   to ensure secrets are never leaked in logs, tracebacks, or JSON dumps.
3. Subsystem Tuning: Provides declarative configuration parameters for:
   - Durable messaging queues (inbox worker counts, polling intervals, lease timeouts).
   - Adaptive model selection (Ollama concurrency limits, cache TTLs).
   - Autonomous tool execution (call caps, output truncation bounds, sandbox root).
   - Network security (loopback-only binding enforcement, machine API keys).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr, AnyHttpUrl
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Declarative application settings model populated from environment and `.env`.

    Attributes:
        app_host: Network host address for the FastAPI backend (defaults to 127.0.0.1).
        app_port: TCP port for the FastAPI backend (defaults to 8000).
        log_level: Logging verbosity (DEBUG, INFO, WARNING, ERROR).
        whatsapp_phone: Primary owner phone number for companion pairing.
        telegram_bot_token: Authentication token from Telegram's @BotFather.
        ollama_model: Default fallback model identifier (e.g. dolphin3:8b).
        ollama_url: Base URL of the local Ollama daemon (defaults to http://127.0.0.1:11434).
        database_url: SQLAlchemy async SQLite connection string.
        session_secret: High-entropy secret for JWT signing and session encryption.
        admin_users: Comma-separated list of authorized administrator usernames.
        admin_password_hash: BCrypt hash for administrative dashboard/CLI login.
        rate_limit_per_minute: Maximum requests allowed per IP address per minute.
    """
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
        """Parsed list of administrator usernames allowed to access management endpoints."""
        if not self.admin_users:
            return []
        return [item.strip() for item in self.admin_users.split(",") if item.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        """Parsed list of trusted origins authorized for cross-origin browser requests."""
        if not self.cors_origins:
            return []
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def is_loopback_only(self) -> bool:
        """Check whether the configured application host is strictly bound to local loopback."""
        host = self.app_host.lower().strip()
        return host in ("127.0.0.1", "localhost", "::1")

    @property
    def valid_api_keys(self) -> list[str]:
        """Parsed list of authorized machine API keys extracted securely from SecretStr."""
        if not self.api_keys:
            return []
        raw = self.api_keys.get_secret_value() if hasattr(self.api_keys, "get_secret_value") else str(self.api_keys)
        return [k.strip() for k in raw.split(",") if k.strip()]


settings = Settings()
