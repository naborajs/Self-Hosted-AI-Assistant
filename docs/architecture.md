# Architecture Guide

## Overview

The assistant is built as a modular FastAPI application with separate services for WhatsApp, Telegram, AI, memory, tools, and persistence.

## Components

- `main.py` – orchestrates startup and shutdown of services.
- `config.py` – central configuration via environment variables.
- `database/` – async SQLAlchemy models and repository operations using SQLite.
- `memory/` – persistent storage for user facts, preferences, and conversation summaries.
- `ai/` – Ollama client for model inference, conversation builder, and tool registry.
- `whatsapp/` – companion-device session management and bridge integration.
- `telegram/` – Telegram bot gateway with async message handling.
- `tools/` – extensible tool implementations for calculator, search, weather, file management, and domain assistants.
- `api/` – secure admin endpoints and memory operations.

## Message flow

1. Incoming message is received from WhatsApp or Telegram.
2. The appropriate gateway resolves the user and conversation.
3. Conversation history is loaded from the database.
4. Prompt is generated and sent to Ollama.
5. AI response is stored and returned to the user.

## Persistence

- Conversations and messages are stored in SQLite.
- Memory records persist long-term with search and update operations.
- WhatsApp session data is encrypted and written to disk.

## Security

- JWT-based admin authentication.
- Environment-driven secrets.
- SQLAlchemy ORM prevents injection.
- Encrypted storage for bridge sessions.
- Rate limiting configured via env.
