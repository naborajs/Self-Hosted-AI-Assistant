# Setup Guide

This guide helps you prepare the environment, install dependencies, and start the Self-Hosted AI Assistant for the first time.

## Prerequisites

- Python 3.11+
- Node.js 18+ and npm
- Ollama installed and running locally
- A registered Telegram bot token
- A WhatsApp phone number for companion pairing

## Step-by-step Setup

1. Clone the repository:

```bash
git clone https://github.com/naborajs/Self-Hosted-AI-Assistant.git
cd Self-Hosted-AI-Assistant
```

2. Create a virtual environment:

```bash
python -m venv .venv
.\.venv\Scripts\activate
```

3. Install Python dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

4. Install the WhatsApp bridge dependencies:

```bash
cd whatsapp_bridge
npm install
```

5. Copy the environment file:

```bash
copy .env.example .env
```

6. Fill in `.env` values and save.

7. Launch Ollama locally before running the backend.

8. Start the WhatsApp bridge:

```bash
npm start
```

9. In the repository root, start the backend:

```bash
python main.py
```

10. Verify the backend at `http://127.0.0.1:8000/health`.
