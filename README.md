# Local AI Assistant

A self-hosted AI assistant designed for WhatsApp-first interaction and Telegram fallback. Powered by Ollama for local model inference, SQLite for persistent memory, and FastAPI for secure admin access.

## Features

- WhatsApp companion-device integration
- Telegram multi-user support
- Ollama local AI model support
- Long-term memory stored in SQLite
- Extensible tool calling system
- Secure authentication and admin controls
- Conversation history with private and group chat support
- Encrypted WhatsApp companion device bridge and auto reconnect flow

## Quickstart

1. Copy `.env.example` to `.env`
2. Fill in `WHATSAPP_PHONE`, `TELEGRAM_BOT_TOKEN`, `OLLAMA_MODEL`, and `SESSION_SECRET`
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Start the WhatsApp bridge in `whatsapp_bridge`:

```bash
cd whatsapp_bridge
npm install
npm start
```

5. Run the Python app:

```bash
python main.py
```

6. Access the API at `http://127.0.0.1:8000/docs`

## Project Structure

- `main.py` – application entrypoint
- `config.py` – environment and runtime configuration
- `database/` – SQLite models and repository layer
- `ai/` – Ollama client, conversation context, tool registry
- `whatsapp/` – WhatsApp bridge and encrypted session storage
- `telegram/` – Telegram bot gateway
- `memory/` – business memory persistence
- `tools/` – built-in assistant tools
- `api/` – REST endpoints and authentication
- `docs/` – installation and architecture documentation
- `tests/` – automated test suite
