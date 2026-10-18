# Project EDITH v2.0 — Local Deployment & Operations Guide

## 1. Running the Services

Project EDITH consists of two local processes:
1. **Python FastAPI Backend:** Runs on `127.0.0.1:8000`.
2. **Node.js WhatsApp Bridge:** Runs on `127.0.0.1:3000` (optional, only needed when using WhatsApp).

### Option A — Automated Scripts (Windows)

Use the preconfigured batch scripts in the repository root:

- **Start Services:**
  ```cmd
  start.bat
  ```
  Launches Ollama verification, starts the WhatsApp bridge in a separate minimized window, and starts the FastAPI server.
- **Stop Services:**
  ```cmd
  stop.bat
  ```
  Gracefully terminates the background Python and Node.js bridge processes.
- **Restart Services:**
  ```cmd
  restart.bat
  ```

### Option B — Manual Execution

**Terminal 1 — Local Ollama Service:**
```bash
ollama serve
```

**Terminal 2 — WhatsApp Bridge:**
```bash
cd whatsapp_bridge
npm start
```

**Terminal 3 — FastAPI Backend Daemon:**
```bash
python main.py
```

---

## 2. Managing EDITH via Operator CLI

The `edith` command provides an operator interface directly in your terminal:

```bash
# Check service health and live statistics
edith status

# Run full system diagnostics
edith doctor

# View installed models and currently configured active default
edith models list
edith models status

# Change the active default model without service restart
edith models set-default dolphin3:8b

# View registered users
edith users list

# Inspect durable message queues
edith queue status
edith queue failed

# Retry a failed outbound message delivery
edith queue retry <message_id>

# Check channel integration status (WhatsApp & Telegram)
edith integrations status
```

For programmatic script integration, append `--json` to any command (e.g. `edith status --json`).

---

## 3. Machine Chat API Usage

External automation scripts and authorized local programs interact with EDITH via the Machine Chat API:

**Endpoint:** `POST /api/v1/chat`
**Headers:**
- `X-API-Key: <your_secret_api_key>`
- `Content-Type: application/json`

**Payload:**
```json
{
  "message": "Calculate the compound interest on $5000 at 5% for 3 years.",
  "chat_id": "automation_thread_01"
}
```

**Response:**
```json
{
  "response": "The compound interest on $5000 at 5% compounded annually for 3 years is approximately $788.13 (Total: $5788.13).",
  "model_used": "dolphin3:8b",
  "conversation_id": 12,
  "client_id": "client_8f93a2...",
  "status": "success"
}
```

---

## 4. Production Hardening Checklist

- [x] **Loopback Binding:** Ensure `APP_HOST=127.0.0.1` and `ALLOW_REMOTE_API=false`.
- [x] **Shared Secrets:** Configure matching non-empty `WHATSAPP_BRIDGE_SECRET` in `.env` and `whatsapp_bridge/.env`.
- [x] **Database Location:** Store `assistant.db` on a local SSD with WAL mode enabled.
- [x] **Backup Strategy:** Regularly back up `assistant.db` and the `storage/` directory.
- [x] **Safe Tool Workspace:** Verify `storage/workspace/` is strictly isolated from source code and credentials.
