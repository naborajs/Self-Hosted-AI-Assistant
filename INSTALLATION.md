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

## Environment Variables

See [CONFIGURATION.md](./CONFIGURATION.md) for the full environment reference.

## First Run

1. Start Ollama.
2. Start the WhatsApp bridge:

```bash
npm start
```

3. In the main project root, run:

```bash
python main.py
```

## Validation

Open the API docs at `http://127.0.0.1:8000/docs` and confirm you can access the health endpoint.
