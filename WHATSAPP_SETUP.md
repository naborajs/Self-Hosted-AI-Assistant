# WhatsApp Setup

This project uses a WhatsApp companion bridge to send and receive messages through your local WhatsApp session.

## Requirements

- Node.js 18+
- A WhatsApp phone number with companion pairing enabled
- `SESSION_SECRET` configured in `.env`

## Start the WhatsApp Bridge

From the `whatsapp_bridge` folder:

```bash
cd whatsapp_bridge
npm install
npm start
```

## Pairing

1. Open the bridge pairing endpoint in your browser:

```text
http://127.0.0.1:3000/qr
```

2. Scan the QR code using WhatsApp Companion on your phone.
3. Wait until the bridge reports `connected`.

## Bridge Health

Visit `http://127.0.0.1:3000/health` to confirm the bridge is running.

## Troubleshooting

- If pairing fails, restart the bridge and refresh the QR page.
- If the backend cannot connect, verify `WHATSAPP_BRIDGE_URL` in `.env`.
- Check the bridge logs for `messages.upsert received` and `sendMessageToChat success`.
