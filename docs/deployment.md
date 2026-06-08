# Deployment Guide

## Local deployment

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Ensure Ollama is running locally and accessible at `OLLAMA_URL`.

3. Ensure your WhatsApp companion bridge is running at `WHATSAPP_BRIDGE_URL`.

4. Start the application:

```bash
python main.py
```

## Production tips

- Run behind a process supervisor such as `systemd`, `pm2`, or Docker.
- Use a reverse proxy like NGINX if exposing the API externally.
- Use HTTPS/TLS for external access.
- Keep the SQLite file on durable storage.
- Monitor logs and restart on failure.

## Docker

This repository does not include a Dockerfile by default, but the service can be containerized by installing dependencies, mounting `.env`, and exposing the configured port.
