# Project EDITH v2.0 — Configuration Guide

This document describes all environment variables supported by Project EDITH v2.0 in `config.py`.

Configurations are loaded from the root `.env` file via Pydantic `BaseSettings`. All sensitive secrets are parsed into `SecretStr` objects to prevent accidental logging or terminal leakage.

---

## Configuration Variable Reference

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| **Network & Core Host** | | | |
| `APP_HOST` | String | `127.0.0.1` | Binding address for the FastAPI backend. Must be `127.0.0.1` or `localhost` unless `ALLOW_REMOTE_API=true`. |
| `APP_PORT` | Integer | `8000` | Port on which the FastAPI application listens. |
| `ALLOW_REMOTE_API` | Boolean | `false` | Explicit opt-in required to bind `APP_HOST` to `0.0.0.0` or non-loopback interfaces. |
| `RATE_LIMIT_PER_MINUTE` | Integer | `60` | Sliding window HTTP rate limit per client IP. |
| `CORS_ORIGINS` | String | `""` | Comma-delimited list of allowed CORS browser origins. |
| **Security & Authentication** | | | |
| `SESSION_SECRET` | SecretStr | *Required* | High-entropy secret (minimum 32 characters) used for JWT HMAC signing and session encryption keys. |
| `ADMIN_USERS` | String | `""` | Comma-delimited list of authorized administrator usernames or IDs. |
| `ADMIN_PASSWORD_HASH` | SecretStr | `None` | Bcrypt password hash for the administrative operator. Generated using `edith config hash-password`. |
| `API_KEYS` | SecretStr | `None` | Comma-delimited machine API keys or `client_id:secret` pairs for machine authentication on `/api/v1/chat`. |
| **Local Ollama Inference Engine** | | | |
| `OLLAMA_URL` | String | `http://127.0.0.1:11434` | Base HTTP endpoint for the local Ollama daemon. |
| `OLLAMA_MODEL` | String | `qwen3:8b` | Fallback default model name. Must match a model installed locally on the host machine. |
| `OLLAMA_MODEL_CACHE_TTL` | Float | `60.0` | Cache time-to-live in seconds for discovered models via `/api/tags`. |
| `OLLAMA_MAX_CONCURRENCY` | Integer | `3` | Maximum simultaneous inference requests allowed through the semaphore. |
| `ALLOW_USER_MODEL_SELECTION` | Boolean | `false` | When true, non-admin users may switch models via `/model <name>`. When false, restricted to admins. |
| **Database & Persistence** | | | |
| `DATABASE_URL` | String | `sqlite+aiosqlite:///./assistant.db` | SQLAlchemy asynchronous database connection string. |
| `STORAGE_PATH` | Path | `./storage` | Directory where personas, knowledge files, and presets are saved. |
| `ENCRYPTED_SESSIONS_PATH` | Path | `./storage/sessions` | Directory where encrypted session files are stored. |
| **Inbound Messaging Inbox Queue** | | | |
| `INBOX_WORKER_COUNT` | Integer | `3` | Number of concurrent worker tasks processing incoming messaging batches. |
| `INBOX_MAX_BATCH_SIZE` | Integer | `50` | Maximum messages claimed per polling cycle. |
| `INBOX_MAX_ATTEMPTS` | Integer | `3` | Maximum worker execution attempts before an inbound message transitions to `FAILED`. |
| `INBOX_LEASE_TIMEOUT` | Float | `120.0` | Lease duration in seconds before a stalled processing lock expires. |
| `INBOX_POLL_INTERVAL` | Float | `1.0` | Frequency in seconds to poll for new inbound messages. |
| `INBOX_MAX_PENDING` | Integer | `200` | Backpressure limit for pending messages. |
| **Tool Execution & Safety Sandbox** | | | |
| `ENABLE_TOOL_EXECUTION` | Boolean | `true` | Master toggle for AI function/tool execution. |
| `TOOL_MAX_ROUNDS` | Integer | `3` | Maximum conversational tool calling loops per dialogue turn. |
| `TOOL_MAX_CALLS_PER_REQUEST` | Integer | `5` | Maximum individual tool calls permitted within a single turn. |
| `TOOL_TIMEOUT_SECONDS` | Float | `15.0` | Timeout in seconds for individual tool execution. |
| `TOOL_MAX_OUTPUT_CHARS` | Integer | `2000` | Truncation limit for raw tool outputs fed back into model prompts. |
| `ALLOW_EXTERNAL_NETWORK_TOOLS` | Boolean | `false` | Enables external network tools (DuckDuckGo web search, wttr.in weather). Disabled by default. |
| `TOOL_DEFAULT_PERMISSION_POLICY` | String | `standard` | Permission policy mode (`standard` or `strict`). |
| `TOOL_WORKSPACE_ROOT` | Path | `./storage/workspace` | Absolute boundary path for sandboxed filesystem tools (`file_manager`). |
| **Messaging Channel Gateways** | | | |
| `WHATSAPP_PHONE` | String | `""` | Primary phone number for WhatsApp companion pairing. |
| `WHATSAPP_BRIDGE_URL` | String | `http://127.0.0.1:3000` | HTTP endpoint for the Node.js Baileys companion bridge. |
| `WHATSAPP_BRIDGE_SECRET` | SecretStr | `None` | Shared secret header (`X-Bridge-Secret`) for authenticating Python-to-Bridge requests. |
| `TELEGRAM_BOT_TOKEN` | String | `""` | Telegram Bot API token obtained from @BotFather. |

---

## Production Security Best Practices

1. **Keep `ALLOW_REMOTE_API=false`:** Always run EDITH bound to `127.0.0.1`. If external access is required, place it behind a local reverse proxy (like NGINX or Caddy) handling TLS/HTTPS termination.
2. **Generate Strong Secrets:** Use a high-entropy string for `SESSION_SECRET`:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
3. **Use Hashed Admin Passwords:** Never store plaintext passwords in `.env`. Hash the admin password using bcrypt:
   ```bash
   python -c "import bcrypt; print(bcrypt.hashpw(b'YourPasswordHere', bcrypt.gensalt()).decode())"
   ```
4. **Never Check `.env` into Git:** Ensure `.env` remains in `.gitignore`.
