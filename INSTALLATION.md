# Installation Guide

## Requirements

- Python 3.11 or later
- Node.js 18 or later
- npm package manager
- Ollama local server
- WhatsApp companion device access
- Telegram Bot token

## Python Installation

1. Create and activate the virtual environment:

```bash
python -m venv .venv
.\.venv\Scripts\activate
```

2. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Node / WhatsApp Bridge Installation

1. Change into the bridge folder:

```bash
cd whatsapp_bridge
```

2. Install dependencies:

```bash
npm install
```

## Environment Variables & Model Setup

1. Copy `.env.example` to `.env`.
2. Verify existing Ollama service: run `ollama list` to inspect models already installed on the machine.
3. Configure `OLLAMA_MODEL` in `.env` to match your preferred installed model.
   - EDITH adapts to whichever compatible model the operator provides and will **never** automatically pull, download, or preload models.
   - Operators may independently choose to install additional models with `ollama pull <name>`, but this is optional and operator-initiated.
4. See [CONFIGURATION.md](./CONFIGURATION.md) for the full environment reference.

## First Run

1. Start or verify the Ollama service (`ollama serve` or system daemon).
2. Verify model availability with the CLI:
   ```bash
   python -m edith doctor
   ```
3. Start the WhatsApp bridge (if using WhatsApp):
   ```bash
   cd whatsapp_bridge
   npm start
   ```
4. In the main project root, start the Python backend:
   ```bash
   python main.py
   ```
   (On Windows, `start.bat` automates this startup sequence).

## Validation

- Check system health: `GET http://127.0.0.1:8000/health`
- Check CLI status: `python -m edith status`
- Explore API documentation: `http://127.0.0.1:8000/docs`
