# WhatsApp Setup & Durable Inbound Pipeline

This project uses a Node.js companion bridge (@whiskeysockets/baileys) coupled with a Python FastAPI backend to reliably send and receive WhatsApp messages through your companion session.

---

## Requirements

- Node.js 18+
- Python 3.10+
- A WhatsApp phone number with companion pairing enabled
- `SESSION_SECRET` configured in `.env`
- `WHATSAPP_BRIDGE_SECRET` configured in `.env`

---

## Architecture: Durable Inbound Pipeline (Phase 2)

```
[ WhatsApp Network ]
         │ (Baileys socket)
         ▼
[ whatsapp_bridge ]
   ├── Inbound Mutex & Deduplication (Stable Fallback IDs)
   ├── Atomic File Persistence: whatsapp_bridge/inbound_queue.json
   └── Non-destructive GET /poll?limit=50
         ▲
         │ (HTTP Long-Poll + Backpressure)
         ▼
[ Python WhatsAppGateway ]
   ├── 1. Ingest & Commit: SQLite `inbound_messages` Table
   ├── 2. Two-Phase ACK: POST /ack (Only after DB commit!)
   ├── 3. Bounded Worker Pool (Default: 3 concurrent workers)
   │     ├── Async Claim Lock & Active Chat Exclusion (Per-Chat FIFO)
   │     ├── Stage 1: LLM Inference -> Status `RESPONSE_READY`
   │     └── Stage 2: Save Assistant Msg -> Enqueue Delivery -> Status `COMPLETED`
   └── 4. Startup Recovery: Interrupted leases recovered automatically
```

### Key Components

1. **Persistent Bridge Queue (`inbound_queue.json`)**
   - Incoming messages received via `messages.upsert` are atomically written to `whatsapp_bridge/inbound_queue.json` using temporary files (`.tmp`) and file renames.
   - Operations are serialized using an asynchronous mutex (`withQueueLock`) to prevent race conditions during concurrent incoming events.
   - Missing IDs are assigned stable fallback SHA-256 hashes (`fb_<hash>`), and duplicates are suppressed on insertion.
   - `GET /poll` is non-destructive (returns an array slice up to `?limit=50`).
   - Messages are removed only when the backend explicitly acknowledges them via authenticated `POST /ack`.

2. **Two-Phase Acknowledgment**
   - The Python backend polls messages from `/poll` and durably commits them into SQLite before sending `POST /ack`.
   - If Python crashes before acknowledgment or the ACK network call times out, the bridge redelivers the messages on the next poll.
   - The backend repository suppresses duplicates using the unique constraint `(platform, external_id)` and returns previously acknowledged IDs safely without duplication.

3. **Durable Inbox Table (`inbound_messages`)**
   - Schema tracks: `id`, `platform`, `external_id`, `chat_id`, `sender`, `sender_name`, `content`, `message_type`, `file_url`, `mime_type`, `caption`, `status`, `attempt_count`, `last_error`, `response_text`, `lease_expires_at`, `created_at`, `updated_at`.
   - Indexed on `(chat_id, status)` and constrained uniquely on `(platform, external_id)`.

4. **Bounded Worker Pool & Strict Per-Chat FIFO**
   - A configurable pool of `INBOX_WORKER_COUNT` workers runs concurrently.
   - Independent conversations are processed in parallel across workers.
   - Messages within the same conversation are strictly serialized: an in-memory `active_chats` set and database lease query prevent multiple workers from claiming messages from the same chat simultaneously.

5. **Staged Message Processing**
   - **Stage 1 (RECEIVED)**: Worker claims message and updates status to `PROCESSING` with a lease timeout (`INBOX_LEASE_TIMEOUT`). The AI model generates a response and commits it to `response_text` with status `RESPONSE_READY`.
   - **Stage 2 (RESPONSE_READY)**: The assistant response is committed to the conversation history, forwarded to the outgoing delivery queue, and marked `COMPLETED`.
   - **Crash Resilience**: If a process restarts after model inference but before outbound delivery, the worker resumes from `RESPONSE_READY` without re-running Ollama inference.

6. **Backpressure Flow Control**
   - Polling automatically pauses when pending inbox records exceed `INBOX_MAX_PENDING` (default 200), preventing queue bloat and memory exhaustion during traffic spikes or slow inference.

---

## Configuration Reference

Add or customize these settings in your `.env` file:

| Variable | Default | Description |
|---|---|---|
| `WHATSAPP_BRIDGE_URL` | `http://127.0.0.1:3000` | Base URL of the Node.js Baileys companion bridge |
| `WHATSAPP_BRIDGE_SECRET` | *(auto/required)* | Shared secret for backend-bridge mutual authentication |
| `INBOX_WORKER_COUNT` | `3` | Number of concurrent background inbox processing workers |
| `INBOX_MAX_BATCH_SIZE` | `50` | Maximum messages retrieved per `/poll` request |
| `INBOX_MAX_ATTEMPTS` | `3` | Maximum processing retries before marking an item `FAILED` |
| `INBOX_LEASE_TIMEOUT` | `120.0` | Seconds before a stale `PROCESSING` lease can be reclaimed |
| `INBOX_POLL_INTERVAL` | `1.0` | Interval in seconds between bridge poll requests |
| `INBOX_MAX_PENDING` | `200` | Backpressure threshold for pending inbound messages |

---

## Starting the Bridge

From the `whatsapp_bridge` directory:

```bash
cd whatsapp_bridge
npm install
npm start
```

## Pairing

1. Open the bridge pairing endpoint in your local browser:
   ```text
   http://127.0.0.1:3000/qr-display
   ```
2. Open WhatsApp on your phone > **Linked Devices** > **Link a Device**.
3. Scan the QR code displayed in your browser.
4. Verify that the browser page and bridge terminal show `connected`.

## Health & Verification

Check the bridge health:
```bash
curl http://127.0.0.1:3000/health
```

Run bridge security & durable queue automated test suites:
```bash
node whatsapp_bridge/test_bridge_security.js
node whatsapp_bridge/test_bridge_queue.js
```

---

## Data Privacy & Security Notes

- **Bridge Queue Storage**: `whatsapp_bridge/inbound_queue.json` stores message metadata and text payloads locally on disk until acknowledged. This file is excluded from Git tracking via `.gitignore`.
- **Permissions**: Restrict file system permissions on `whatsapp_bridge/inbound_queue.json` and `storage/` on shared multi-user workstations.
- **Decommissioning**: To fully wipe pending queue contents and session credentials, delete `whatsapp_bridge/inbound_queue.json` and `whatsapp_bridge/storage/`.
