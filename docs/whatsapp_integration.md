# WhatsApp Integration

This project includes a dedicated Node.js WhatsApp bridge that uses Baileys for companion-device authentication.

## Workflow

1. The bridge starts and loads encrypted WhatsApp credentials from local storage.
2. If no credentials exist, the bridge generates a pairing QR and exposes it via `/pair` and `/pairing_code`.
3. The Python app polls the bridge for incoming WhatsApp events at `/poll`.
4. Incoming messages are routed through Ollama and the assistant replies automatically.
5. Outgoing messages are enqueued and delivered through the bridge.
6. Session state is encrypted and persisted in `storage/whatsapp_bridge`.

## Bridge endpoints

- `GET /health` - health check
- `GET /status` - connection and pairing status
- `POST /pair` - initiate pairing flow
- `GET /pairing_code` - return current pairing QR code data
- `GET /poll` - retrieve new WhatsApp events
- `POST /send-message` - send text or media
- `POST /typing` - emit typing indicators
- `POST /logout` - clear WhatsApp session
- `GET /media/:fileName` - retrieve received media

## Storage

- Auth credentials are encrypted with `SESSION_SECRET`.
- Media files are stored under `storage/whatsapp_bridge/media`.
- `WHATSAPP_BRIDGE_STORAGE` configures the bridge storage path.
