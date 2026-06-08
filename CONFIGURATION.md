# Configuration Reference

## Environment Variables

| Variable | Description | Example |
|---|---|---|
| `APP_HOST` | API host | `127.0.0.1` |
| `APP_PORT` | API port | `8000` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `WHATSAPP_PHONE` | WhatsApp phone number | `918900653250` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` |
| `OLLAMA_MODEL` | Ollama model identifier | `qwen3:8b` |
| `OLLAMA_URL` | Ollama server URL | `http://127.0.0.1:11434` |
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite+aiosqlite:///./assistant.db` |
| `SESSION_SECRET` | Secret for admin auth and encrypted storage | `supersecret` |
| `ADMIN_USERS` | Comma-separated admin usernames | `admin` |
| `RATE_LIMIT_PER_MINUTE` | API rate limit | `60` |
| `WHATSAPP_BRIDGE_URL` | Local bridge endpoint | `http://127.0.0.1:3000` |
| `WHATSAPP_BRIDGE_HOST` | WhatsApp bridge host | `127.0.0.1` |
| `WHATSAPP_BRIDGE_PORT` | WhatsApp bridge port | `3000` |

## Files and Storage

The project uses `storage/` for AI configuration and runtime data:

- `storage/ai_identity.json`
- `storage/presets.json`
- `storage/rules.json`
- `storage/system_prompts/`
- `storage/personalities/`
- `storage/knowledge/`

## AI Identity Settings

The AI identity file controls the assistant's persona and branding.

Example section from `storage/ai_identity.json`:

```json
{
  "ai_name": "EDITH",
  "ai_personality": "Friendly",
  "ai_role": "Self-hosted assistant",
  "owner_name": "Nishant Sarkar",
  "brand_name": "NS Gamming"
}
```

## Prompt Overrides

The system prompts are stored as separate JSON files under `storage/system_prompts/`.

- `global.json`
- `whatsapp.json`
- `telegram.json`
- `admin.json`
- `templates.json`

## Personalities

Personalities are loaded from `storage/personalities/`.
Each personality file is a JSON document describing tone, role, and speaking style.

## Knowledge Base

Knowledge items are stored in `storage/knowledge/index.json` and can be updated through the admin API.
