# Troubleshooting Guide

## Common issues

### App fails to start

- Verify `.env` exists and contains required values.
- Confirm Python 3.12+ is installed.
- Check for dependency issues with `pip install -r requirements.txt`.

### Ollama connection errors

- Confirm `OLLAMA_URL` is correct.
- Ensure Ollama is running locally.
- Test the endpoint with a simple HTTP request.

### Telegram bot does not respond

- Confirm `TELEGRAM_BOT_TOKEN` is valid.
- Ensure the bot has been started and can receive messages.
- Check application logs for connection errors.

### WhatsApp pairing issues

- Ensure the bridge service at `WHATSAPP_BRIDGE_URL` is available.
- Validate the companion pairing flow with the bridge service.
- Confirm session files are stored in the `storage/sessions` directory.

### Database errors

- Check `DATABASE_URL` for correct SQLite path.
- Ensure the application can create the database file.
- Delete corrupted DB and restart if necessary.

## Logs

- Review logs printed by `main.py` for startup and runtime details.
- Use `LOG_LEVEL=DEBUG` to surface additional information.
