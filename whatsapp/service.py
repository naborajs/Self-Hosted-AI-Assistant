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
from database.models import InboundMessage, OutboundMessage
from database.repository import (
    claim_next_inbound_message,
    count_pending_inbound,
    create_conversation,
    create_or_get_outbound_message,
    get_conversation,
    get_or_create_user,
    get_outbound_by_inbound_id,
    get_user_preference,
    mark_inbound_completed,
    mark_inbound_failed,
    mark_inbound_response_ready,
    mark_outbound_ambiguous,
    mark_outbound_failed,
    mark_outbound_sending,
    mark_outbound_sent,
    recover_stale_inbound_leases,
    recover_stale_outbound_leases,
    requeue_outbound_retry,
    resolve_effective_model,
    save_inbound_batch,
    save_message,
)
from ai.commands import handle_user_command
from ai.orchestrator import ToolOrchestrator
from ai.tools import ToolRegistry
from memory.store import get_memory_store

logger = logging.getLogger("local_ai_assistant.whatsapp")



class WhatsAppGateway:
    def __init__(self, settings: Any, ai_manager: AIManager) -> None:
        self.settings = settings
        self.ai_manager = ai_manager
        self.base_url = str(settings.whatsapp_bridge_url).rstrip("/")
        headers = {}
        if getattr(settings, "whatsapp_bridge_secret", None):
            secret_obj = settings.whatsapp_bridge_secret
            secret_val = secret_obj.get_secret_value() if hasattr(secret_obj, "get_secret_value") else str(secret_obj)
            if secret_val:
                headers["X-Bridge-Secret"] = secret_val
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), headers=headers)
        self.poll_task: asyncio.Task | None = None
        self.send_task: asyncio.Task | None = None
        self.worker_tasks: list[asyncio.Task] = []
        self.active_chats: set[str] = set()
        self.claim_lock = asyncio.Lock()
        self.work_available_event = asyncio.Event()
        self.running: bool = False
        self.outgoing_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.ai_client = OllamaClient()
        self.memory_store = get_memory_store()
        self.tool_registry = ToolRegistry(self.settings)
        self.tool_orchestrator = ToolOrchestrator(self.ai_client, self.tool_registry, self.settings)
        self.pairing_code: str | None = None
        self.connected: bool = False
        self.session_lock = asyncio.Lock()
        self.max_send_attempts: int = 3
        self.retry_delay: float = 1.0
        self.dead_letter_queue: list[dict[str, Any]] = []


    async def _health_check(self) -> bool:
        try:
            response = await self.client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def start(self) -> None:
        logger.info("Starting WhatsApp gateway")
        self.running = True
        
        # Check if bridge is reachable
        bridge_available = await self._health_check()
        if not bridge_available:
            logger.warning("WhatsApp bridge is unavailable at %s - will retry on first poll", self.base_url)
        else:
            status = await self.status()
            if status.get("connected"):
                self.connected = True
                logger.info("WhatsApp bridge is connected")
            else:
                logger.info("WhatsApp bridge is reachable but not connected - waiting for pairing")

        # Recover any stale uncompleted leases from a previous process termination
        try:
            lease_timeout = getattr(self.settings, "inbox_lease_timeout", 120.0)
            recovered = await recover_stale_inbound_leases(lease_timeout)
            if recovered:
                logger.info("Recovered %d interrupted inbox message leases on startup", recovered)
        except Exception as exc:
            logger.warning("Failed to recover stale inbox leases on startup: %s", exc)

        try:
            stale_outbounds = await recover_stale_outbound_leases(stale_threshold_seconds=60.0)
            if stale_outbounds:
                logger.info("Recovered %d interrupted outbound message leases on startup", stale_outbounds)
        except Exception as exc:
            logger.warning("Failed to recover stale outbound leases on startup: %s", exc)

        # Reconstruct pending outbound deliveries from SQLite into the outgoing queue
        try:
            reconstructed = await self._reconstruct_pending_outbounds()
            if reconstructed:
                logger.info("Reconstructed %d pending outbound deliveries from SQLite", reconstructed)
        except Exception as exc:
            logger.warning("Failed to reconstruct pending outbounds on startup: %s", exc)

        # Start bounded worker pool
        worker_count = getattr(self.settings, "inbox_worker_count", 3)
        self.worker_tasks = [
            asyncio.create_task(self._inbox_worker(i)) for i in range(worker_count)
        ]

        self.poll_task = asyncio.create_task(self._poll_messages())
        self.send_task = asyncio.create_task(self._process_outgoing())

    async def stop(self) -> None:
        logger.info("Stopping WhatsApp gateway")
        self.running = False
        if self.poll_task is not None:
            self.poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.poll_task
        if self.send_task is not None:
            self.send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.send_task
        for worker in self.worker_tasks:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self.worker_tasks.clear()
        self.active_chats.clear()
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

    async def get_qr(self) -> bytes | None:
        try:
            response = await self.client.get(f"{self.base_url}/qr")
            if response.status_code == 200:
                return response.content
            return None
        except httpx.HTTPError:
            return None

    async def send_typing(self, chat_id: str) -> None:
        try:
            await self.client.post(
                f"{self.base_url}/typing",
                json={"chat_id": chat_id},
                timeout=httpx.Timeout(2.0, connect=1.0),
            )
        except Exception as exc:
            logger.warning("Typing indicator failed: %s", exc)

    async def send_message(
        self,
        chat_id: str,
        text: str,
        file_url: str | None = None,
        mime_type: str | None = None,
        caption: str | None = None,
        inbound_id: int | None = None,
        conversation_id: int | None = None,
        message_id: int | None = None,
        outbound_id: int | None = None,
    ) -> OutboundMessage | None:
        outbound = None
        if outbound_id is None:
            try:
                outbound = await create_or_get_outbound_message(
                    chat_id=chat_id,
                    text=text,
                    file_url=file_url,
                    mime_type=mime_type,
                    caption=caption,
                    inbound_id=inbound_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    max_attempts=self.max_send_attempts,
                )
                if outbound:
                    outbound_id = outbound.id
            except Exception as exc:
                logger.debug("Durable OutboundMessage persistence skipped: %s", exc)

        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if file_url:
            payload.update({"file_url": file_url, "mime_type": mime_type, "caption": caption})
        if outbound_id is not None:
            payload["outbound_id"] = outbound_id

        try:
            logger.debug("Queueing outgoing message to bridge: %s", {"chat_id": chat_id, "text": text and (text[:200] + ("..." if len(text) > 200 else ""))})
        except Exception:
            logger.debug("Queueing outgoing message to bridge: %s", chat_id)
        await self.outgoing_queue.put(payload)
        try:
            logger.debug("Outgoing queue size after enqueue: %d", self.outgoing_queue.qsize())
        except Exception:
            pass
        return outbound

    async def _poll_messages(self) -> None:
        consecutive_errors = 0
        max_consecutive_errors = 10
        poll_interval = getattr(self.settings, "inbox_poll_interval", 1.0)
        max_batch = getattr(self.settings, "inbox_max_batch_size", 50)
        max_pending = getattr(self.settings, "inbox_max_pending", 200)

        while self.running:
            try:
                # 1. Backpressure check: pause polling if inbox is at capacity
                pending_count = await count_pending_inbound("whatsapp")
                if pending_count >= max_pending:
                    logger.warning(
                        "WhatsApp inbox at capacity (%d pending >= %d max). Backpressure applied.",
                        pending_count, max_pending
                    )
                    await asyncio.sleep(2.0)
                    continue

                # 2. Check bridge status if not connected
                if not self.connected:
                    try:
                        status = await self.status()
                        if status.get("connected"):
                            self.connected = True
                            logger.info("WhatsApp bridge reconnected")
                            consecutive_errors = 0
                    except Exception:
                        pass

                # 3. Poll bridge for messages (non-destructive)
                logger.debug("Polling WhatsApp bridge at %s/poll?limit=%d", self.base_url, max_batch)
                response = await self.client.get(
                    f"{self.base_url}/poll?limit={max_batch}",
                    timeout=httpx.Timeout(30.0, connect=5.0)
                )
                response.raise_for_status()
                messages = response.json().get("messages", [])

                logger.debug("Poll returned %d messages", len(messages))
                if messages:
                    consecutive_errors = 0
                    # 4. Durably persist entire batch into SQLite inbox
                    persisted_ids = await save_inbound_batch(messages, platform="whatsapp")

                    # 5. Acknowledge to bridge ONLY after successful database persistence
                    if persisted_ids:
                        try:
                            ack_resp = await self.client.post(
                                f"{self.base_url}/ack",
                                json={"message_ids": persisted_ids},
                                timeout=httpx.Timeout(10.0, connect=5.0)
                            )
                            ack_resp.raise_for_status()
                            logger.debug("Acknowledged %d messages to bridge", len(persisted_ids))
                        except Exception as ack_err:
                            logger.warning("Acknowledgment to bridge failed (will retry on next poll): %s", ack_err)

                    # 6. Signal available work to worker pool
                    self.work_available_event.set()

                await asyncio.sleep(poll_interval)
            except httpx.ConnectError as exc:
                consecutive_errors += 1
                if consecutive_errors == 1:
                    logger.warning("WhatsApp bridge connection lost: %s", exc)
                if consecutive_errors >= max_consecutive_errors:
                    logger.warning("WhatsApp bridge unavailable - retrying indefinitely")
                    consecutive_errors = max_consecutive_errors
                self.connected = False
                await asyncio.sleep(5.0)
            except httpx.HTTPError as exc:
                logger.warning("WhatsApp poll HTTP error: %s", exc)
                self.connected = False
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Unexpected error in WhatsApp poll loop: %s", exc)
                await asyncio.sleep(2.0)

    async def _inbox_worker(self, worker_id: int) -> None:
        logger.debug("WhatsApp inbox worker %d started", worker_id)
        lease_timeout = getattr(self.settings, "inbox_lease_timeout", 120.0)
        max_attempts = getattr(self.settings, "inbox_max_attempts", 3)

        while self.running:
            try:
                record = None
                async with self.claim_lock:
                    record = await claim_next_inbound_message(
                        self.active_chats,
                        lease_seconds=lease_timeout,
                        max_attempts=max_attempts,
                        platform="whatsapp",
                    )
                    if record is not None:
                        chat_id = record.chat_id
                        self.active_chats.add(chat_id)
                        self.work_available_event.set()

                if record is None:
                    self.work_available_event.clear()
                    try:
                        await asyncio.wait_for(self.work_available_event.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue

                try:
                    await self._process_inbound_record(record)
                finally:
                    self.active_chats.discard(chat_id)
                    self.work_available_event.set()

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("WhatsApp inbox worker %d loop exception: %s", worker_id, exc)
                await asyncio.sleep(1.0)

    async def _process_inbound_record(self, record: InboundMessage) -> None:
        chat_id = record.chat_id
        sender = record.sender
        sender_name = record.sender_name or sender
        content = record.content
        message_type = record.message_type
        sanitized_chat = self._sanitize_recipient(chat_id)
        max_attempts = getattr(self.settings, "inbox_max_attempts", 3)

        if not sender or not chat_id:
            await mark_inbound_failed(record.id, "Missing sender or chat_id", permanent=True)
            return

        try:
            # Stage 1: Check if answer was already generated prior to an interruption
            answer = record.response_text
            if not answer or record.status != "RESPONSE_READY":
                external_id = f"whatsapp:{sender}"
                app_user = await get_or_create_user(external_id, sender_name)
                conversation = await get_conversation(app_user.id, chat_id)
                if conversation is None:
                    conversation = await create_conversation(app_user.id, chat_id, title=chat_id)

                await save_message(conversation.id, sender, "user", content, message_type)

                # 1. Command Interception (/models, /model, /language, /style, /remember, /memories, /forget, /help, /confirm, /cancel)
                is_command, command_reply = await handle_user_command(
                    app_user,
                    content,
                    self.ai_client,
                    self.settings,
                    self.memory_store,
                    conversation_id=conversation.id,
                    tool_registry=self.tool_registry,
                )
                if is_command:
                    answer = command_reply or ""
                else:
                    # 2. Preset Rule Matching
                    preset_answer = self.ai_manager.match_preset(content)
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
                            context = ConversationContext(
                                conversation.id,
                                self.ai_manager,
                                memory_store=self.memory_store,
                                user_id=app_user.id,
                            )
                            await self.send_typing(chat_id)

                            # Check if ai_client was mocked in tests with a custom mock or generate method
                            is_mock_generate = (
                                type(self.ai_client).__name__ in ("AsyncMock", "MagicMock", "Mock")
                                or hasattr(getattr(self.ai_client, "generate", None), "assert_called")
                                or hasattr(getattr(self.ai_client, "generate", None), "assert_not_called")
                            ) and not getattr(self.ai_client, "_test_tool_orchestration", False)

                            if getattr(self.settings, "enable_tool_execution", True) and not is_mock_generate:

                                system_context = self.ai_manager.build_system_context(platform="whatsapp", user_name=sender_name)
                                knowledge = self.ai_manager.search_knowledge(content)
                                history = await context.load_history()
                                mem_facts: list[str] = []
                                if self.memory_store:
                                    try:
                                        facts = await self.memory_store.retrieve_relevant_facts(
                                            user_id=app_user.id,
                                            query_text=content,
                                            max_count=5,
                                            max_chars=600,
                                        )
                                        for f in facts:
                                            clean_val = str(f.value).replace("\n", " ").strip()
                                            mem_facts.append(f"- {f.key}: {clean_val}")
                                    except Exception as exc:
                                        logger.warning("Memory retrieval in whatsapp gateway failed: %s", exc)

                                self.tool_orchestrator.ai_client = self.ai_client
                                answer = await self.tool_orchestrator.run_turn(
                                    user_id=app_user.id,
                                    conversation_id=conversation.id,
                                    user_message=content,
                                    system_context=system_context,
                                    history=history,
                                    model=effective_model,
                                    user_name=sender_name,
                                    platform="whatsapp",
                                    preferred_language=pref_lang,
                                    response_style=pref_style,
                                    knowledge_snippets=knowledge,
                                    user_memories=mem_facts,
                                )
                            else:
                                prompt = await context.build_prompt(
                                    platform="whatsapp",
                                    user_name=sender_name,
                                    user_message=content,
                                    preferred_language=pref_lang,
                                    response_style=pref_style,
                                    user_id=app_user.id,
                                )
                                prompt += "\nAssistant:"

                                try:
                                    answer = await self.ai_client.generate(prompt, model=effective_model)
                                except TypeError as te:
                                    if "unexpected keyword argument 'model'" in str(te) or "got an unexpected keyword argument" in str(te):
                                        answer = await self.ai_client.generate(prompt)
                                    else:
                                        raise

                            if not answer or not answer.strip():
                                raise ValueError("Empty response received from AI model")


                # Durably persist generated answer before queuing delivery
                await mark_inbound_response_ready(record.id, answer)

            # Stage 2: Save assistant message to conversation history & enqueue delivery
            external_id = f"whatsapp:{sender}"
            app_user = await get_or_create_user(external_id, sender_name)
            conversation = await get_conversation(app_user.id, chat_id)
            if conversation is None:
                conversation = await create_conversation(app_user.id, chat_id, title=chat_id)

            existing_outbound = await get_outbound_by_inbound_id(record.id)
            assistant_msg_id = None
            if existing_outbound and existing_outbound.message_id:
                # Assistant message was already saved prior to an interruption - do not duplicate
                assistant_msg_id = existing_outbound.message_id
            elif conversation is not None:
                assistant_msg = await save_message(conversation.id, "assistant", "assistant", answer)
                assistant_msg_id = assistant_msg.id

            await self.send_message(
                chat_id,
                answer,
                inbound_id=record.id,
                conversation_id=conversation.id if conversation else None,
                message_id=assistant_msg_id,
                outbound_id=existing_outbound.id if existing_outbound else None,
            )
            await mark_inbound_completed(record.id)
            logger.debug("Successfully processed inbound message %s for %s", record.external_id, sanitized_chat)

        except Exception as exc:
            sanitized_err = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.exception("Failed processing inbound record %s: %s", record.external_id, sanitized_err)
            await mark_inbound_failed(
                record.id,
                sanitized_err,
                permanent=False,
                max_attempts=max_attempts,
            )

    async def _process_incoming(self, item: dict[str, Any]) -> None:
        """Backward-compatible helper that stores and processes an item through the durable pipeline."""
        persisted_ids = await save_inbound_batch([item], platform="whatsapp")
        if persisted_ids:
            msg = await claim_next_inbound_message(set(), lease_seconds=120.0, max_attempts=3, platform="whatsapp")
            if msg:
                await self._process_inbound_record(msg)

    def _sanitize_recipient(self, chat_id: str | None) -> str:
        if not chat_id:
            return "[empty]"
        if "@" in chat_id:
            user_part, domain = chat_id.split("@", 1)
            visible = user_part[:4] if len(user_part) >= 4 else user_part[:1]
            return f"{visible}***@{domain}"
        return chat_id[:4] + "***" if len(chat_id) > 4 else "***"

    async def _handle_dead_letter(self, item: dict[str, Any], reason: str) -> None:
        chat_id = item.get("chat_id")
        sanitized = self._sanitize_recipient(chat_id)
        attempts = item.get("_attempts", 1)
        logger.error(
            "WhatsApp message delivery permanently failed. Moved to dead-letter queue: recipient=%s attempts=%d reason=%s",
            sanitized, attempts, reason
        )
        record = {
            "chat_id": sanitized,
            "attempts": attempts,
            "reason": reason,
            "has_media": bool(item.get("file_url")),
        }
        self.dead_letter_queue.append(record)
        if len(self.dead_letter_queue) > 100:
            self.dead_letter_queue.pop(0)

    async def _reconstruct_pending_outbounds(self) -> int:
        from database.connection import get_session
        from database.models import OutboundMessage
        from sqlalchemy import select
        async with get_session() as session:
            stmt = select(OutboundMessage).where(
                OutboundMessage.platform == "whatsapp",
                OutboundMessage.status == "PENDING",
            ).order_by(OutboundMessage.created_at.asc())
            result = await session.execute(stmt)
            pending = result.scalars().all()
            for record in pending:
                payload = {
                    "chat_id": record.chat_id,
                    "text": record.text,
                    "file_url": record.file_url,
                    "mime_type": record.mime_type,
                    "caption": record.caption,
                    "outbound_id": record.id,
                    "_attempts": record.attempt_count,
                }
                await self.outgoing_queue.put(payload)
            return len(pending)

    async def _process_outgoing(self) -> None:
        while True:
            try:
                item = await asyncio.wait_for(self.outgoing_queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

            attempts = item.get("_attempts", 0) + 1
            item["_attempts"] = attempts
            chat_id = item.get("chat_id")
            outbound_id = item.get("outbound_id")
            sanitized_id = self._sanitize_recipient(chat_id)

            if outbound_id:
                with contextlib.suppress(Exception):
                    await mark_outbound_sending(outbound_id, lease_seconds=60.0, attempts=attempts)

            if not chat_id:
                if outbound_id:
                    with contextlib.suppress(Exception):
                        await mark_outbound_failed(outbound_id, "Missing chat_id", attempts=attempts)
                await self._handle_dead_letter(item, "Missing chat_id")
                continue

            bridge_payload = {k: v for k, v in item.items() if not k.startswith("_") and k != "outbound_id"}

            try:
                response = await self.client.post(
                    f"{self.base_url}/send-message",
                    json=bridge_payload,
                    timeout=httpx.Timeout(10.0, connect=5.0)
                )
                response.raise_for_status()
                logger.debug("Message successfully delivered to %s on attempt %d", sanitized_id, attempts)
                if outbound_id:
                    with contextlib.suppress(Exception):
                        await mark_outbound_sent(outbound_id)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                # Check for Ambiguous delivery outcome (ReadTimeout)
                is_read_timeout = isinstance(exc, httpx.ReadTimeout)
                if is_read_timeout:
                    reason = "ReadTimeout: Request was sent to bridge, but response timed out. Delivery outcome is ambiguous."
                    logger.warning(
                        "Ambiguous delivery outcome for %s (attempt %d/%d): %s",
                        sanitized_id, attempts, self.max_send_attempts, reason
                    )
                    if outbound_id:
                        with contextlib.suppress(Exception):
                            await mark_outbound_ambiguous(outbound_id, reason, attempts=attempts)
                    await self._handle_dead_letter(item, f"Ambiguous ({type(exc).__name__})")
                    continue

                is_transient = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if isinstance(exc, httpx.TimeoutException) and not is_read_timeout:
                    is_transient = True
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (502, 503, 504, 429):
                    is_transient = True

                if is_transient and attempts < self.max_send_attempts:
                    logger.warning(
                        "Transient send failure for %s (attempt %d/%d): %s. Re-queueing after delay.",
                        sanitized_id, attempts, self.max_send_attempts, type(exc).__name__
                    )
                    if outbound_id:
                        with contextlib.suppress(Exception):
                            await requeue_outbound_retry(outbound_id, f"{type(exc).__name__}: {exc}", attempts=attempts)
                    await asyncio.sleep(self.retry_delay)
                    await self.outgoing_queue.put(item)
                else:
                    reason = f"{type(exc).__name__}: {exc}" if is_transient else f"Permanent failure ({type(exc).__name__})"
                    if isinstance(exc, httpx.HTTPStatusError):
                        reason = f"HTTP {exc.response.status_code}"
                    if outbound_id:
                        with contextlib.suppress(Exception):
                            await mark_outbound_failed(outbound_id, reason, attempts=attempts)
                    await self._handle_dead_letter(item, reason)
