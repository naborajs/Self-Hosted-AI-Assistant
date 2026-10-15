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

logger = logging.getLogger("local_ai_assistant.telegram")


class TelegramGateway:
    def __init__(self, settings: Any, memory: Any, ai_manager: AIManager) -> None:
        self.settings = settings
        self.memory = memory
        self.ai_manager = ai_manager
        self.bot_token = settings.telegram_bot_token
        self.application = Application.builder().token(self.bot_token).build()
        self.ai_client = OllamaClient()

        self.application.add_handler(CommandHandler("start", self.handle_start))
        self.application.add_handler(CommandHandler(["models", "model", "language", "style", "remember", "memories", "forget", "help"], self.handle_command))
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
            username = user.username or user.full_name
            app_user = await get_or_create_user(external_id, username)
            conversation = await get_conversation(app_user.id, str(chat.id))
            if conversation is None:
                conversation = await create_conversation(app_user.id, str(chat.id), title=chat.title or username)

            text = update.message.text
            await save_message(conversation.id, username, "user", text)

            is_cmd, reply = await handle_user_command(app_user, text, self.ai_client, self.settings, self.memory)
            answer = reply or "Command processed."
            await save_message(conversation.id, "assistant", "assistant", answer)
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
            username = user.username or user.full_name
            app_user = await get_or_create_user(external_id, username)
            conversation = await get_conversation(app_user.id, str(chat.id))
            if conversation is None:
                conversation = await create_conversation(app_user.id, str(chat.id), title=chat.title or username)

            text = update.message.text
            logger.debug("Telegram message from %s: %s", username, text and (text[:200] + ("..." if len(text) > 200 else "")))
            await save_message(conversation.id, username, "user", text)

            # 1. Command Check (e.g. if sent without leading command filter)
            is_cmd, cmd_reply = await handle_user_command(app_user, text, self.ai_client, self.settings, self.memory)
            if is_cmd:
                answer = cmd_reply or ""
            else:
                # 2. Preset Match
                preset_answer = self.ai_manager.match_preset(text)
                if preset_answer:
                    answer = preset_answer
                else:
                    # 3. Model Resolution & Personalized Inference
                    pref = await get_user_preference(app_user.id)
                    pref_lang = pref.preferred_language if pref else None
                    pref_style = pref.response_style if pref else None

                    model_names = []
                    is_reachable = True
                    try:
                        installed_models = await self.ai_client.get_installed_models()
                        if isinstance(installed_models, (list, tuple)):
                            model_names = [m.name for m in installed_models if hasattr(m, "name")]
                            if not model_names:
                                avail_result = await self.ai_client.is_ollama_available()
                                is_reachable = bool(avail_result) if isinstance(avail_result, bool) else True
                        else:
                            model_names = [getattr(self.settings, "ollama_model", "qwen3:8b")]
                    except Exception:
                        is_reachable = False

                    effective_model, source, model_error = await resolve_effective_model(
                        app_user.id,
                        model_names,
                        default_env_model=getattr(self.settings, "ollama_model", "qwen3:8b"),
                        ollama_reachable=is_reachable,
                    )

                    if model_error:
                        answer = f"⚠️ {model_error}"
                    else:
                        context_obj = ConversationContext(
                            conversation.id,
                            self.ai_manager,
                            memory_store=self.memory,
                            user_id=app_user.id,
                        )
                        prompt = await context_obj.build_prompt(
                            platform="telegram",
                            user_name=username,
                            user_message=text,
                            preferred_language=pref_lang,
                            response_style=pref_style,
                            user_id=app_user.id,
                        )
                        prompt += "\nAssistant:"
                        try:
                            try:
                                answer = await self.ai_client.generate(prompt, model=effective_model)
                            except TypeError as te:
                                if "unexpected keyword argument 'model'" in str(te) or "got an unexpected keyword argument" in str(te):
                                    answer = await self.ai_client.generate(prompt)
                                else:
                                    raise
                            if not answer or not answer.strip():
                                answer = "I couldn't generate a response. Please try again."
                        except Exception as exc:
                            logger.exception("Ollama generation failed: %s", exc)
                            answer = f"I encountered an error processing your request: {exc}"

            await save_message(conversation.id, "assistant", "assistant", answer)
            logger.debug("Sending Telegram reply to %s: %s", username, answer and (answer[:200] + ("..." if len(answer) > 200 else "")))
            await update.message.reply_text(answer)
        except Exception as exc:
            logger.exception("Failed to handle Telegram message: %s", exc)
            try:
                await update.message.reply_text("An error occurred processing your message. Please try again.")
            except Exception:
                pass
