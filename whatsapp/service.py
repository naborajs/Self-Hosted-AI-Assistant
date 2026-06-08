from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from ai.client import OllamaClient
from ai.conversation import ConversationContext
from ai.manager import AIManager
from config import settings
from database.repository import create_conversation, get_conversation, get_or_create_user, save_message

logger = logging.getLogger("local_ai_assistant.whatsapp")


class WhatsAppGateway:
    def __init__(self, settings: Any, ai_manager: AIManager) -> None:
        self.settings = settings
        self.ai_manager = ai_manager
        self.base_url = str(settings.whatsapp_bridge_url).rstrip("/")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self.poll_task: asyncio.Task | None = None
        self.send_task: asyncio.Task | None = None
        self.outgoing_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.ai_client = OllamaClient()
        self.pairing_code: str | None = None
        self.connected: bool = False
        self.session_lock = asyncio.Lock()

    async def _health_check(self) -> bool:
        try:
            response = await self.client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def start(self) -> None:
        logger.info("Starting WhatsApp gateway")
        
        # Check if bridge is reachable
        bridge_available = await self._health_check()
        if not bridge_available:
            logger.warning("WhatsApp bridge is unavailable at %s - will retry on first poll", self.base_url)
        else:
            # Check connection status
            status = await self.status()
            if status.get("connected"):
                self.connected = True
                logger.info("WhatsApp bridge is connected")
            else:
                logger.info("WhatsApp bridge is reachable but not connected - waiting for pairing")
        
        # Start polling and sending tasks regardless of bridge status
        # They will handle disconnection gracefully and reconnect when available
        self.poll_task = asyncio.create_task(self._poll_messages())
        self.send_task = asyncio.create_task(self._process_outgoing())

    async def stop(self) -> None:
        logger.info("Stopping WhatsApp gateway")
        if self.poll_task is not None:
            self.poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.poll_task
        if self.send_task is not None:
            self.send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.send_task
        await self.client.aclose()

    async def status(self) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{self.base_url}/status")
            response.raise_for_status()
            status = response.json()
            self.connected = status.get("connected", False)
            return status
        except httpx.HTTPError:
            return {"connected": False}

    async def pair_device(self, phone_number: str) -> dict[str, Any]:
        async with self.session_lock:
            response = await self.client.post(
                f"{self.base_url}/pair",
                json={"phone_number": phone_number},
            )
            response.raise_for_status()
            payload = response.json()
            self.pairing_code = payload.get("pairing_code")
            self.connected = payload.get("connected", False)
            return payload

    async def logout(self) -> dict[str, Any]:
        response = await self.client.post(f"{self.base_url}/logout")
        response.raise_for_status()
        self.connected = False
        return response.json()

    async def send_typing(self, chat_id: str) -> None:
        try:
            await self.client.post(f"{self.base_url}/typing", json={"chat_id": chat_id})
        except httpx.HTTPError as exc:
            logger.warning("Typing indicator failed: %s", exc)

    async def send_message(
        self,
        chat_id: str,
        text: str,
        file_url: str | None = None,
        mime_type: str | None = None,
        caption: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if file_url:
            payload.update({"file_url": file_url, "mime_type": mime_type, "caption": caption})
        try:
            logger.debug("Queueing outgoing message to bridge: %s", {"chat_id": chat_id, "text": text and (text[:200] + ("..." if len(text) > 200 else ""))})
        except Exception:
            logger.debug("Queueing outgoing message to bridge: %s", chat_id)
        await self.outgoing_queue.put(payload)
        try:
            logger.debug("Outgoing queue size after enqueue: %d", self.outgoing_queue.qsize())
        except Exception:
            pass

    async def _process_incoming(self, item: dict[str, Any]) -> None:
        logger.debug("_process_incoming called with item: %s", item)
        chat_id = item.get("chat_id")
        sender = item.get("sender") or item.get("sender_id")
        sender_name = item.get("sender_name") or sender
        content = item.get("content", "")
        message_type = item.get("message_type", "text")
        if not sender or not chat_id:
            logger.debug("Skipping invalid incoming WhatsApp item (missing sender or chat_id): %s", item)
            return

        logger.debug("Incoming message from %s in chat %s: %s", sender, chat_id, content and (content[:200] + ("..." if len(content) > 200 else "")))

        try:
            external_id = f"whatsapp:{sender}"
            app_user = await get_or_create_user(external_id, sender_name)
            conversation = await get_conversation(app_user.id, chat_id)
            if conversation is None:
                conversation = await create_conversation(app_user.id, chat_id, title=item.get("chat_title", chat_id))

            await save_message(conversation.id, sender, "user", content, message_type)
            context = ConversationContext(conversation.id, self.ai_manager)

            preset_answer = self.ai_manager.match_preset(content)
            if preset_answer:
                answer = preset_answer
            else:
                prompt = await context.build_prompt(platform="whatsapp", user_name=sender_name, user_message=content)
                prompt += f"\nAssistant:"

                await self.send_typing(chat_id)
                try:
                    answer = await self.ai_client.generate(prompt)
                    if not answer or not answer.strip():
                        answer = "I couldn't generate a response. Please try again."
                except Exception as exc:
                    logger.exception("Ollama generation failed: %s", exc)
                    answer = "I encountered an error processing your request. Please try again."

            await save_message(conversation.id, "assistant", "assistant", answer)
            logger.debug("Queueing assistant response to outgoing queue for chat %s", chat_id)
            await self.send_message(chat_id, answer)
        except Exception as exc:
            logger.exception("Failed to process incoming WhatsApp message: %s", exc)
            try:
                await self.send_message(chat_id, "An error occurred processing your message. Please try again.")
            except Exception:
                pass

    async def _poll_messages(self) -> None:
        consecutive_errors = 0
        max_consecutive_errors = 10
        
        while True:
            try:
                # If not connected, try to check status first
                if not self.connected:
                    try:
                        status = await self.status()
                        if status.get("connected"):
                            self.connected = True
                            logger.info("WhatsApp bridge reconnected")
                            consecutive_errors = 0
                    except Exception:
                        pass  # Will try again next iteration
                
                # Poll for messages
                logger.debug("Polling WhatsApp bridge at %s/poll", self.base_url)
                response = await self.client.get(f"{self.base_url}/poll", timeout=httpx.Timeout(30.0, connect=5.0))
                response.raise_for_status()
                messages = response.json().get("messages", [])

                logger.debug("Poll returned %d messages", len(messages))
                if messages:
                    consecutive_errors = 0  # Reset on successful poll

                for item in messages:
                    logger.debug("WhatsApp event: %s", item)
                    await self._process_incoming(item)
                
                await asyncio.sleep(1.0)
            except httpx.ConnectError as exc:
                consecutive_errors += 1
                if consecutive_errors == 1:
                    logger.warning("WhatsApp bridge connection lost: %s", exc)
                if consecutive_errors >= max_consecutive_errors:
                    logger.warning("WhatsApp bridge unavailable - retrying indefinitely")
                    consecutive_errors = max_consecutive_errors  # Cap at max
                self.connected = False
                await asyncio.sleep(5.0)
            except httpx.HTTPError as exc:
                logger.warning("WhatsApp poll HTTP error: %s", exc)
                self.connected = False
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Unexpected WhatsApp poll error: %s", exc)
                await asyncio.sleep(5.0)

    async def _process_outgoing(self) -> None:
        while True:
            try:
                item = await asyncio.wait_for(self.outgoing_queue.get(), timeout=30.0)
                try:
                    logger.debug("Sending to bridge /send-message: %s", {"chat_id": item.get("chat_id"), "text": item.get("text") and (item.get("text")[:200] + ("..." if len(item.get("text"))>200 else ""))})
                    response = await self.client.post(f"{self.base_url}/send-message", json=item, timeout=httpx.Timeout(10.0, connect=5.0))
                    logger.debug("Bridge /send-message response status: %s", getattr(response, 'status_code', None))
                    logger.debug("Message sent successfully to %s", item.get("chat_id"))
                except httpx.ConnectError as exc:
                    logger.warning("Cannot send message - bridge offline: %s", exc)
                    # Re-queue for retry later
                    await self.outgoing_queue.put(item)
                    await asyncio.sleep(5.0)
                except Exception as exc:
                    logger.exception("Failed to send queued WhatsApp message: %s", exc)
                    # Re-queue for retry on other errors
                    await self.outgoing_queue.put(item)
                    await asyncio.sleep(2.0)
            except asyncio.TimeoutError:
                # Timeout waiting for queue item, that's ok - just continue
                continue
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Unexpected error in outgoing message processor: %s", exc)
                await asyncio.sleep(2.0)
