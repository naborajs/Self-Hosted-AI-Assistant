# Project EDITH v2.0 — Troubleshooting & Diagnostics Guide

This guide assists operators in diagnosing and resolving runtime issues with Project EDITH.

---

## 1. Automated Health Diagnostics (`edith doctor`)

The fastest way to troubleshoot is running `edith doctor`:

```bash
edith doctor
```

`edith doctor` checks:
1. **Environment Configuration:** Validates `.env` variables and secret parameters.
2. **Database Engine:** Checks SQLite WAL mode, database read/write permissions, and table migrations.
3. **Ollama Reachability:** Tests connectivity to `http://127.0.0.1:11434` and lists installed model tags.
4. **Port Availability:** Verifies ports 8000 and 3000 are not hijacked by other processes.
5. **Bridge Diagnostics:** Evaluates the Baileys companion bridge readiness.

---

## 2. Common Issues & Solutions

### A. "Ollama service unreachable" / `OllamaConnectionError`
- **Symptom:** AI generation fails with `Ollama service is unreachable at http://127.0.0.1:11434`.
- **Cause:** The Ollama daemon is not running on the local host.
- **Resolution:**
  1. Open a new terminal and run:
     ```bash
     ollama serve
     ```
  2. Verify it answers HTTP requests:
     ```bash
     curl http://127.0.0.1:11434/api/tags
     ```

### B. "Requested model is not installed in Ollama" / `ModelNotFoundError`
- **Symptom:** User commands fail stating the model is missing.
- **Cause:** EDITH follows a strict **zero auto-pull policy** and will never download models automatically in the background.
- **Resolution:**
  1. View your installed models:
     ```bash
     ollama list
     ```
  2. Pull the desired model manually using Ollama CLI:
     ```bash
     ollama pull dolphin3:8b
     ```
  3. Or change EDITH's active default model via CLI:
     ```bash
     edith models set-default <installed_model_name>
     ```

### C. WhatsApp Bridge Fails Pairing or Returns 401 Unauthorized
- **Symptom:** Bridge logs report authentication rejected, or Python poller cannot read messages.
- **Cause:** Mismatched `WHATSAPP_BRIDGE_SECRET` between `.env` and `whatsapp_bridge/.env`.
- **Resolution:**
  1. Ensure `WHATSAPP_BRIDGE_SECRET` in `.env` matches `BRIDGE_SECRET` or `WHATSAPP_BRIDGE_SECRET` in `whatsapp_bridge/.env`.
  2. For companion pairing, open `http://127.0.0.1:3000/qr-display` in your local browser to scan the QR code. Note that `/qr-display` is restricted strictly to local loopback requests with anti-CSRF checks.

### D. Telegram Gateway Reports "Invalid token"
- **Symptom:** Warning logged: `Telegram gateway initialization skipped (invalid token)`.
- **Cause:** Placeholder `your-telegram-bot-token` or invalid token supplied in `TELEGRAM_BOT_TOKEN`.
- **Resolution:**
  - This is **non-fatal**: the backend continues running normally for WhatsApp and Machine API clients.
  - To enable Telegram, message @BotFather on Telegram, create a bot, and place the actual bot token into `TELEGRAM_BOT_TOKEN` in `.env`.

### E. SQLite Database Locked or High Concurrency Timeouts
- **Symptom:** `sqlite3.OperationalError: database is locked`.
- **Cause:** Another process holds an exclusive write lock, or WAL mode is disabled.
- **Resolution:**
  1. Ensure no rogue Python instances are running:
     ```cmd
     stop.bat
     ```
  2. Verify WAL mode is enabled:
     ```bash
     edith doctor
     ```
  3. Connection pragmas in `database/connection.py` automatically set `busy_timeout=5000` and `journal_mode=WAL`.

### F. High-Privilege Tool Requires Confirmation
- **Symptom:** Tool output states: `Action staged. Please confirm with /confirm <token> within 120 seconds`.
- **Cause:** This is an intentional security safeguard. High-privilege actions (like file writes) generate a two-phase confirmation token.
- **Resolution:**
  - Enter `/confirm <token>` in the chat within 2 minutes.
  - To cancel the action, enter `/cancel <token>` or `/cancel`.
