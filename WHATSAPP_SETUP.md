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

## Architecture: End-to-End Durable Pipeline (Phase 2 & Phase 2.1)

```
[ WhatsApp Network ]
         │ (Baileys socket)
         ▼
[ whatsapp_bridge ]
   ├── Deterministic Local Storage: WHATSAPP_STORAGE_DIR / WHATSAPP_INBOUND_QUEUE_PATH
   ├── Inbound Mutex & Deduplication (Stable Fallback IDs)
   ├── Atomic File Persistence: inbound_queue.json (0o700 dir, 0o600 file)
   ├── Non-destructive GET /poll?limit=50
   └── POST /send-message (Transmits payload to Baileys socket)
         ▲                                   ▲
         │ (HTTP Long-Poll + Backpressure)    │ (Durable Dispatch + Leases)
         ▼                                   │
[ Python WhatsAppGateway ]                   │
   ├── 1. Ingest & Commit: SQLite `inbound_messages` Table
   ├── 2. Two-Phase ACK: POST /ack (Only after DB commit!)
   ├── 3. Bounded Worker Pool (Default: 3 concurrent workers)
   │     ├── Async Claim Lock & Active Chat Exclusion (Per-Chat FIFO)
   │     ├── Stage 1: LLM Inference -> Status `RESPONSE_READY`
   │     └── Stage 2: Save Assistant Msg -> Persist `OutboundMessage` -> Status `COMPLETED`
   ├── 4. Outbound Delivery Worker (`outbound_messages` Table)
   │     ├── State Machine: PENDING -> SENDING -> SENT | AMBIGUOUS | FAILED
   │     ├── Ambiguous Outcome Protection (ReadTimeout dead-letters, avoids duplicate spam)
   │     └── Transient Retry (5xx / connect errors retry up to max_attempts)
   └── 5. Crash Recovery:
         ├── Interrupted inbound leases recovered to RESPONSE_READY or RECEIVED
         ├── Interrupted outbound leases recovered to PENDING without re-inference
         └── Duplicate assistant messages strictly prevented via linked message_id
```

### Key Components

1. **Persistent Bridge Queue (`inbound_queue.json`)**
   - Incoming messages received via `messages.upsert` are atomically written to `inbound_queue.json` using temporary files (`.tmp`) and file renames.
   - Operations are serialized using an asynchronous mutex (`withQueueLock`) to prevent race conditions during concurrent incoming events.
   - Storage directory defaults to `whatsapp_bridge/storage` via deterministic `__dirname` resolution (not vulnerable to `process.cwd()` variance) and can be overridden via `WHATSAPP_STORAGE_DIR` or `WHATSAPP_INBOUND_QUEUE_PATH`.
   - File system permissions are enforced: `0o700` for storage directory, `0o600` for queue files.
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

4. **Durable Outbound Table (`outbound_messages`)**
   - Schema tracks: `id`, `inbound_id`, `conversation_id`, `message_id`, `correlation_id`, `platform`, `chat_id`, `text`, `file_url`, `mime_type`, `caption`, `status`, `attempt_count`, `max_attempts`, `last_error`, `lease_expires_at`, `sent_at`, `created_at`, `updated_at`.
   - Indexed on `(chat_id, status)` and constrained uniquely on `correlation_id`.
   - Outbound Lifecycle:
     - `PENDING`: Persisted in SQLite prior to dispatch.
     - `SENDING`: Leased by the outgoing worker with an active lease timeout.
     - `SENT`: Confirmed delivered by bridge HTTP 200 with recorded `sent_at` timestamp.
     - `AMBIGUOUS`: A network `ReadTimeout` occurred after dispatch. The bridge may have transmitted the message, but the outcome cannot be confirmed. Blind retries are halted to prevent spamming the recipient.
     - `FAILED`: Permanently rejected (e.g. 4xx Client Error) or max attempts exhausted. Moved to dead-letter queue.

5. **Bounded Worker Pool & Strict Per-Chat FIFO**
   - A configurable pool of `INBOX_WORKER_COUNT` workers runs concurrently.
   - Independent conversations are processed in parallel across workers.
   - Messages within the same conversation are strictly serialized: an in-memory `active_chats` set and database lease query prevent multiple workers from claiming messages from the same chat simultaneously.

6. **Staged Message Processing & Duplicate Prevention**
   - **Stage 1 (RECEIVED)**: Worker claims message and updates status to `PROCESSING` with a lease timeout (`INBOX_LEASE_TIMEOUT`). The AI model generates a response and commits it to `response_text` with status `RESPONSE_READY`.
   - **Stage 2 (RESPONSE_READY)**: The assistant response is committed to conversation history and `OutboundMessage` is durably created.
   - **Duplicate History Prevention**: When resuming from a crash, Stage 2 checks `get_outbound_by_inbound_id(record.id)`. If `existing_outbound.message_id` is present, creating a second assistant message in the conversation history is skipped, eliminating duplicate assistant records.

7. **Crash Resilience & Outbound Reconstruction**
   - On startup, `recover_stale_outbound_leases()` reclaims interrupted leases.
   - `_reconstruct_pending_outbounds()` loads all unconfirmed `PENDING` outbounds from SQLite into the outgoing queue without re-running Ollama inference.

---

## Configuration Reference

Add or customize these settings in your `.env` file:

| Variable | Default | Description |
|---|---|---|
| `WHATSAPP_BRIDGE_URL` | `http://127.0.0.1:3000` | Base URL of the Node.js Baileys companion bridge |
| `WHATSAPP_BRIDGE_SECRET` | *(auto/required)* | Shared secret for backend-bridge mutual authentication |
| `WHATSAPP_STORAGE_DIR` | `./storage/whatsapp_bridge` | Directory for bridge auth credentials and inbound queue |
| `WHATSAPP_INBOUND_QUEUE_PATH` | `[storage]/inbound_queue.json` | Explicit file path override for bridge inbound queue |
| `INBOX_WORKER_COUNT` | `3` | Number of concurrent background inbox processing workers |
| `INBOX_MAX_BATCH_SIZE` | `50` | Maximum messages retrieved per `/poll` request |
| `INBOX_MAX_ATTEMPTS` | `3` | Maximum processing retries before marking an item `FAILED` |
| `INBOX_LEASE_TIMEOUT` | `120.0` | Seconds before a stale `PROCESSING` lease can be reclaimed |
| `INBOX_POLL_INTERVAL` | `1.0` | Interval in seconds between bridge poll requests |
| `INBOX_MAX_PENDING` | `200` | Backpressure threshold for pending inbound messages |
| `OLLAMA_MODEL_CACHE_TTL` | `60.0` | Seconds to cache installed Ollama model list |
| `OLLAMA_MAX_CONCURRENCY` | `3` | Maximum simultaneous active Ollama inferences |
| `ALLOW_USER_MODEL_SELECTION` | `false` | When false, only admins can switch models via `/model` |

---

## Interactive In-Chat Commands

Users can interact with the assistant directly via WhatsApp using specialized commands:

- `/models`: Lists all installed Ollama models discovered dynamically via `GET /api/tags` on the host machine.
- `/model`: Displays your active effective model, user preference, and resolution source.
- `/model <model_name>`: Switches your active model (e.g. `/model llama3:8b`). Requires administrator permissions unless `ALLOW_USER_MODEL_SELECTION=true`.
- `/model default` or `/model reset`: Clears your user model preference, reverting to the admin or server default.
- `/language <language>`: Sets your personal language preference (e.g. `/language Spanish`).
- `/language default`: Clears language preference.
- `/style <concise|detailed|normal>`: Customizes reply brevity and style.
- `/style default`: Resets reply style to normal.

### Model Precedence Order
1. **User Preference** (`user_preferences.preferred_model`)
2. **Admin Global Default** (`system_settings.global_default_model`)
3. **Configured Environment Default** (`OLLAMA_MODEL`)

**Explicit Error Guarantee**: If a selected model is missing or uninstalled, the assistant responds with an explicit error notification (`⚠️ Selected model '...' is not installed in Ollama`) and refuses to silently downgrade to an unintended model.

## Docker Volume Mounts

When deploying with Docker Compose, ensure both containers map persistent host directories:

```yaml
services:
  app:
    volumes:
      - ./assistant.db:/app/assistant.db
      - ./storage:/app/storage
  whatsapp_bridge:
    volumes:
      - ./storage/whatsapp_bridge:/app/storage/whatsapp_bridge
```

---

## Truthful Delivery Guarantees

1. **Inbound Guarantee**: **At-least-once delivery with idempotent deduplication**. Inbound messages are preserved across crashes in `inbound_queue.json` until committed to SQLite `inbound_messages`. Redelivered messages are deduplicated by `(platform, external_id)`.
2. **Internal Processing Guarantee**: **Exactly-once state transition per stage**. Inference produces a single `response_text`, and history insertion is deduplicated via `OutboundMessage.message_id`.
3. **Outbound Delivery Guarantee**: **At-least-once attempt with bounded retry and ambiguous state detection**. The application cannot guarantee exactly-once receipt on WhatsApp's proprietary network across arbitrary network splits. When a network timeout occurs while awaiting bridge confirmation, the system marks the message `AMBIGUOUS` and refrains from blind automated retries, protecting users from duplicate message loops.

---

## Operational Runbook & Disaster Recovery

### Handling Process Crashes
1. **Bridge Crash**: On restart, the bridge loads `inbound_queue.json` and resumes the Baileys session from `storage/whatsapp_bridge/auth_info_baileys/`. No unacknowledged messages are lost.
2. **Python Backend Crash**: On startup, the backend automatically executes:
   - `recover_stale_inbound_leases()`: Resets interrupted inbox messages to `RESPONSE_READY` or `RECEIVED`.
   - `recover_stale_outbound_leases()`: Resets interrupted outbound messages to `PENDING`.
   - `_reconstruct_pending_outbounds()`: Re-enqueues pending outbounds into the dispatch worker.

### Resolving Dead-Lettered and Ambiguous Messages
Inspect dead-lettered messages via SQLite:
```sql
SELECT id, chat_id, status, attempt_count, last_error, created_at
FROM outbound_messages
WHERE status IN ('AMBIGUOUS', 'FAILED');
```
- **To manually retry an ambiguous message**:
  ```sql
  UPDATE outbound_messages
  SET status = 'PENDING', attempt_count = 0, last_error = NULL
  WHERE id = <outbound_id>;
  ```
- **To cancel/ignore an ambiguous message**:
  ```sql
  UPDATE outbound_messages
  SET status = 'FAILED', last_error = 'Manually dismissed by operator'
  WHERE id = <outbound_id>;
  ```

### Backup & Restore Guidelines
1. **SQLite Database (`assistant.db`)**:
   - Because the database operates in WAL mode (`journal_mode = WAL`), avoid raw file copies while the backend is running.
   - Use the SQLite backup command:
     ```bash
     sqlite3 assistant.db ".backup 'assistant_backup.db'"
     ```
2. **WhatsApp Bridge Session (`storage/whatsapp_bridge`)**:
   - Back up `storage/whatsapp_bridge/auth_info_baileys/` to preserve companion device pairing without re-scanning QR codes.
   - Keep permissions restricted (`chmod 700 storage/whatsapp_bridge`).

---

## Verification & Automated Tests

Run the complete regression test suite:
```bash
# Python adaptive models, durability, and regression tests (73 tests)
python -m pytest -v -W ignore

# Node.js bridge security & queue tests
node whatsapp_bridge/test_bridge_security.js
node whatsapp_bridge/test_bridge_queue.js
```
