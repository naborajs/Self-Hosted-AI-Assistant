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
    def __init__(self, settings: Any, memory: Any, ai_manager: AIManager) -> None:
        self.settings = settings
        self.memory = memory
        self.ai_manager = ai_manager
        self.bot_token = settings.telegram_bot_token
        self.application = Application.builder().token(self.bot_token).build()
        self.ai_client = OllamaClient()
        self.tool_registry = ToolRegistry(self.settings)
        self.tool_orchestrator = ToolOrchestrator(self.ai_client, self.tool_registry, self.settings)

        self.application.add_handler(CommandHandler("start", self.handle_start))
        self.application.add_handler(CommandHandler(
            ["models", "model", "language", "style", "remember", "memories", "forget", "help", "confirm", "cancel"],
            self.handle_command,
        ))

        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def start(self) -> None:
        logger.info("Starting Telegram gateway")
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()

    async def stop(self) -> None:
        logger.info("Stopping Telegram gateway")
        await self.application.updater.stop_polling()
        await self.application.stop()
        await self.application.shutdown()
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
