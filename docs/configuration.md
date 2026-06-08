# Configuration Guide

## Environment variables

- `WHATSAPP_PHONE`: Companion number used for pairing with WhatsApp.
- `TELEGRAM_BOT_TOKEN`: Telegram bot token for message relay.
- `OLLAMA_MODEL`: Default local Ollama model, e.g. `qwen3:8b`.
- `OLLAMA_URL`: Local Ollama API endpoint.
- `DATABASE_URL`: SQLite database connection string.
- `SESSION_SECRET`: Secret used for JWT and session encryption.
- `ADMIN_USERS`: Comma-separated admin usernames.
- `APP_HOST`: Host address for FastAPI.
- `APP_PORT`: Port for FastAPI.
- `RATE_LIMIT_PER_MINUTE`: Request throttling limit.
- `WHATSAPP_BRIDGE_URL`: Local bridge URL for WhatsApp companion integration.

## Example

```env
WHATSAPP_PHONE=+1234567890
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
OLLAMA_MODEL=qwen3:8b
OLLAMA_URL=http://127.0.0.1:11434
DATABASE_URL=sqlite+aiosqlite:///./assistant.db
SESSION_SECRET=replace-with-strong-secret
ADMIN_USERS=admin@example.com
APP_HOST=127.0.0.1
APP_PORT=8000
RATE_LIMIT_PER_MINUTE=60
WHATSAPP_BRIDGE_URL=http://127.0.0.1:3000
```

## Security guidance

- Keep `.env` out of source control.
- Use a strong `SESSION_SECRET` and rotate as needed.
- Restrict access to the bridge endpoint and local application.
