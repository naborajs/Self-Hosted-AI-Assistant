# Installation Guide

## Requirements

- Python 3.12+
- Ollama installed and running locally
- WhatsApp companion-device bridge available
- Telegram bot token

## Install dependencies

```bash
pip install -r requirements.txt
```

## Setup environment

1. Copy `.env.example` to `.env`.
2. Fill in `WHATSAPP_PHONE`, `TELEGRAM_BOT_TOKEN`, `OLLAMA_MODEL`, and `SESSION_SECRET`.
3. Optionally adjust `DATABASE_URL`, `APP_HOST`, `APP_PORT`, and `RATE_LIMIT_PER_MINUTE`.

## Start the WhatsApp bridge

```bash
cd whatsapp_bridge
npm install
npm start
```

## Initialize the application

```bash
python main.py
```

The app starts FastAPI on the configured host and port. Use the auto-generated OpenAPI docs at `/docs` for API exploration.

## Docker deployment

```bash
docker compose up --build
```
