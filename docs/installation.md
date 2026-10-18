# Project EDITH v2.0 — Installation Guide

This guide details the complete installation procedure for running Project EDITH v2.0 on Windows or Linux workstations.

---

## 1. System Requirements

- **Operating System:** Windows 10/11 (64-bit) or Linux (Ubuntu 22.04+ recommended).
- **Python:** Python 3.10 to 3.12 (Python 3.12+ recommended).
- **Node.js:** Node.js v18.0.0 or higher with npm (required for the WhatsApp companion bridge).
- **Ollama:** Installed locally from [https://ollama.ai](https://ollama.ai).
- **Hardware:** Dedicated GPU recommended (e.g. NVIDIA RTX 3060/4060/5060 or better) with 8GB+ VRAM for local 7B-14B models. CPU inference is supported but slower.

---

## 2. Step-by-Step Installation

### Step 2.1 — Clone the Repository
```bash
git clone https://github.com/naborajs/Self-Hosted-AI-Assistant.git
cd Self-Hosted-AI-Assistant
```

### Step 2.2 — Python Virtual Environment & Dependencies
Create and activate a virtual environment, then install Python requirements and the `edith` CLI package:

```bash
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

### Step 2.3 — Node.js WhatsApp Bridge Setup
Install dependencies for the Baileys companion bridge:

```bash
cd whatsapp_bridge
npm install
cd ..
```

---

## 3. Environment Configuration

Copy the sample environment file to create your local `.env`:

```bash
cp .env.example .env
```

Open `.env` in your editor and configure the essential parameters:
1. **SESSION_SECRET:** Generate a random 32-character string:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. **OLLAMA_MODEL:** Verify which models you have installed using `ollama list` and enter your preferred model (e.g. `dolphin3:8b`, `gemma4:12b`, `llama3:8b`). EDITH will never download or substitute models automatically.
3. **WHATSAPP_BRIDGE_SECRET:** Set a matching bridge secret for secure internal loopback communication.
4. **TELEGRAM_BOT_TOKEN:** (Optional) Enter your bot token from @BotFather if using Telegram. If unused, leave as default placeholder.

---

## 4. Verification with `edith doctor`

Run the built-in diagnostic tool to verify all components before starting services:

```bash
edith doctor
```

`edith doctor` will verify:
- Python version and core library imports.
- SQLite WAL configuration and schema tables.
- Ollama runtime daemon connectivity and installed model inventory.
- Port availability (`8000` for FastAPI, `3000` for the WhatsApp bridge).
- Node.js environment and bridge dependency readiness.
