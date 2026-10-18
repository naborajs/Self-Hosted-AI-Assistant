"""Telegram bot gateway implementation for Project EDITH v2.0.

This module provides the `TelegramGateway` service:
- Fault-Tolerant Lifespan: Validates and initializes the Telegram Application instance.
  Invalid or placeholder tokens (`your-...`) and network handshake errors are caught
  gracefully without terminating the host FastAPI lifespan.
- Command Dispatch: Registers handlers for core commands (`/start`, `/models`, `/model`,
  `/language`, `/style`, `/remember`, `/memories`, `/forget`, `/help`, `/confirm`, `/cancel`).
- Turn Integration: Hands plain text messages directly to `execute_conversation_turn`
  with platform tag `platform="telegram"`, maintaining user identity and memory continuity.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import settings
from database.repository import (
    create_conversation,
    get_conversation,
    get_or_create_user,
    get_user_preference,
    resolve_effective_model,
    save_message,
)
from ai.client import OllamaClient
from ai.commands import handle_user_command
from ai.conversation import ConversationContext
from ai.manager import AIManager
from ai.orchestrator import ToolOrchestrator
from ai.processor import execute_conversation_turn
from ai.tools import ToolRegistry

logger = logging.getLogger("local_ai_assistant.telegram")


class TelegramGateway:
    """Gateway service coordinating Telegram Bot API polling and conversation processing."""
    def __init__(self, settings: Any, memory: Any, ai_manager: AIManager) -> None:
        self.settings = settings
        self.memory = memory
        self.ai_manager = ai_manager
        self.bot_token = getattr(settings, "telegram_bot_token", None) or ""
        self.ai_client = OllamaClient()
        self.tool_registry = ToolRegistry(self.settings)
        self.tool_orchestrator = ToolOrchestrator(self.ai_client, self.tool_registry, self.settings)
        self.application = None

        if self.bot_token and self.bot_token.strip() and not self.bot_token.strip().startswith("your-"):
            try:
                self.application = Application.builder().token(self.bot_token.strip()).build()
                self.application.add_handler(CommandHandler("start", self.handle_start))
                self.application.add_handler(CommandHandler(
                    ["models", "model", "language", "style", "remember", "memories", "forget", "help", "confirm", "cancel"],
                    self.handle_command,
                ))
                self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
            except Exception as exc:
                logger.warning("Telegram gateway initialization skipped (invalid token): %s", exc)
                self.application = None
        else:
            logger.info("Telegram gateway disabled: no valid telegram_bot_token configured.")

    async def start(self) -> None:
        if self.application is None:
            logger.info("Telegram gateway skipped: not configured")
            return
        logger.info("Starting Telegram gateway")
        try:
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling()
        except Exception as exc:
            logger.warning("Telegram gateway failed to start (invalid token or network issue): %s", exc)
            self.application = None

    async def stop(self) -> None:
        logger.info("Stopping Telegram gateway")
        if self.application is not None:
            try:
                if getattr(self.application, "updater", None) and getattr(self.application.updater, "running", False):
                    await self.application.updater.stop_polling()
                await self.application.stop()
                await self.application.shutdown()
            except Exception as exc:
                logger.debug("Telegram shutdown warning: %s", exc)
        if self.ai_client:
            await self.ai_client.close()

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Local AI assistant ready. Send a message to begin, or use /models to view available models.")

    async def handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None or update.message is None:
            return

        try:
            external_id = f"telegram:{user.id}"
            username = user.username or user.full_name or str(user.id)
            text = update.message.text or ""

            turn = await execute_conversation_turn(
                external_id=external_id,
                sender_name=username,
                chat_id=str(chat.id),
                content=text,
                platform="telegram",
                sender_id=username,
                ai_client=self.ai_client,
                ai_manager=self.ai_manager,
                memory_store=self.memory,
                tool_registry=self.tool_registry,
                tool_orchestrator=self.tool_orchestrator,
                app_settings=self.settings,
            )
            answer = turn.response
            await update.message.reply_text(answer)
        except Exception as exc:
            logger.exception("Failed handling Telegram command: %s", exc)
            await update.message.reply_text("An error occurred executing your command. Please try again.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None or update.message is None:
            return

        try:
            logger.debug("Telegram update received: chat_id=%s user_id=%s", getattr(chat, 'id', None), getattr(user, 'id', None))
            external_id = f"telegram:{user.id}"
            username = user.username or user.full_name or str(user.id)
            text = update.message.text or ""
            logger.debug("Telegram message from %s: %s", username, text and (text[:200] + ("..." if len(text) > 200 else "")))

            turn = await execute_conversation_turn(
                external_id=external_id,
                sender_name=username,
                chat_id=str(chat.id),
                content=text,
                platform="telegram",
                sender_id=username,
                ai_client=self.ai_client,
                ai_manager=self.ai_manager,
                memory_store=self.memory,
                tool_registry=self.tool_registry,
                tool_orchestrator=self.tool_orchestrator,
                app_settings=self.settings,
            )
            answer = turn.response
            logger.debug("Sending Telegram reply to %s: %s", username, answer and (answer[:200] + ("..." if len(answer) > 200 else "")))
            await update.message.reply_text(answer)
        except Exception as exc:
            logger.exception("Failed to handle Telegram message: %s", exc)
            try:
                await update.message.reply_text("An error occurred processing your message. Please try again.")
            except Exception:
                pass
