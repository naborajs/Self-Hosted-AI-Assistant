from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import settings
from database.repository import create_conversation, get_or_create_user, get_conversation, save_message
from ai.client import OllamaClient
from ai.conversation import ConversationContext

logger = logging.getLogger("local_ai_assistant.telegram")


class TelegramGateway:
    def __init__(self, settings: Any, memory: Any) -> None:
        self.settings = settings
        self.memory = memory
        self.bot_token = settings.telegram_bot_token
        self.application = Application.builder().token(self.bot_token).build()
        self.ai_client = OllamaClient()

        self.application.add_handler(CommandHandler("start", self.handle_start))
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
        await update.message.reply_text("Local AI assistant ready. Send a message to begin.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return

        try:
            logger.debug("Telegram update received: chat_id=%s user_id=%s", getattr(chat, 'id', None), getattr(user, 'id', None))
            external_id = f"telegram:{user.id}"
            username = user.username or user.full_name
            app_user = await get_or_create_user(external_id, username)
            conversation = await get_conversation(app_user.id, str(chat.id))
            if conversation is None:
                conversation = await create_conversation(app_user.id, str(chat.id), title=chat.title or username)

            text = update.message.text
            logger.debug("Telegram message from %s: %s", username, text and (text[:200] + ("..." if len(text) > 200 else "")))
            await save_message(conversation.id, username, "user", text)
            context_obj = ConversationContext(conversation.id)
            prompt = await context_obj.build_prompt()
            prompt += f"\nUser: {text}\nAssistant:"

            try:
                answer = await self.ai_client.generate(prompt)
                if not answer or not answer.strip():
                    answer = "I couldn't generate a response. Please try again."
            except Exception as exc:
                logger.exception("Ollama generation failed: %s", exc)
                answer = "I encountered an error processing your request. Please try again."
            
            await save_message(conversation.id, "assistant", "assistant", answer)
            logger.debug("Sending Telegram reply to %s: %s", username, answer and (answer[:200] + ("..." if len(answer) > 200 else "")))
            await update.message.reply_text(answer)
        except Exception as exc:
            logger.exception("Failed to handle Telegram message: %s", exc)
            try:
                await update.message.reply_text("An error occurred processing your message. Please try again.")
            except Exception:
                pass
