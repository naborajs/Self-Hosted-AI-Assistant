import asyncio
import contextlib
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ai.manager import AIManager
from database.connection import init_db, shutdown_db, get_session
from database.models import InboundMessage, Conversation, Message, User
from database.repository import (
    claim_next_inbound_message,
    count_pending_inbound,
    get_inbound_message,
    mark_inbound_completed,
    mark_inbound_failed,
    mark_inbound_response_ready,
    recover_stale_inbound_leases,
    save_inbound_batch,
    list_messages,
)
from whatsapp.service import WhatsAppGateway


def create_mock_ai_manager():
    m = MagicMock(spec=AIManager)
    m.build_system_context.return_value = "System context: You are EDITH."
    m.search_knowledge.return_value = []
    m.match_preset.return_value = None
    return m


class MockGatewaySettings:
    def __init__(self, db_url: str):
        self.database_url = db_url
        self.whatsapp_bridge_url = "http://127.0.0.1:3000"
        self.whatsapp_bridge_secret = "test-bridge-secret"
        self.whatsapp_phone = "+1234567890"
        self.inbox_worker_count = 3
        self.inbox_max_batch_size = 50
        self.inbox_max_attempts = 3
        self.inbox_lease_timeout = 120.0
        self.inbox_poll_interval = 0.05
        self.inbox_max_pending = 200


@contextlib.asynccontextmanager
async def create_isolated_inbound_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite+aiosqlite:///{path}"
    settings = MockGatewaySettings(db_url)
    await init_db(settings)
    try:
        yield {"path": path, "db_url": db_url, "settings": settings}
    finally:
        await shutdown_db()
        for ext in ["", "-wal", "-shm"]:
            fpath = path + ext
            if os.path.exists(fpath):
                with contextlib.suppress(OSError):
                    os.remove(fpath)


# ============================================================================
# Phase 2 Test Scenarios (Tasks 2, 3, 4, 5, 6)
# ============================================================================

@pytest.mark.asyncio
async def test_db_persistence_failure_prevents_ack():
    """Scenario 6: Database persistence failure prevents acknowledgment to bridge."""
    async with create_isolated_inbound_db() as env:
        gateway = WhatsAppGateway(env["settings"], create_mock_ai_manager())
        gateway.running = True

        mock_poll_response = AsyncMock()
        mock_poll_response.raise_for_status = lambda: None
        mock_poll_response.json = lambda: {
            "messages": [
                {
                    "message_id": "msg_db_fail_1",
                    "chat_id": "111@s.whatsapp.net",
                    "sender": "111",
                    "content": "Test fail",
                }
            ]
        }

        # Mock client: GET /poll succeeds, but save_inbound_batch fails
        gateway.client.get = AsyncMock(return_value=mock_poll_response)
        gateway.client.post = AsyncMock()

        # Patch whatsapp.service.save_inbound_batch to simulate an operational database exception
        import whatsapp.service
        original_save = whatsapp.service.save_inbound_batch

        async def failing_save(*args, **kwargs):
            raise RuntimeError("Simulated SQLite write error / disk full")

        whatsapp.service.save_inbound_batch = failing_save
        try:
            # Run a single poll step
            poll_task = asyncio.create_task(gateway._poll_messages())
            await asyncio.sleep(0.08)
            gateway.running = False
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        finally:
            whatsapp.service.save_inbound_batch = original_save

        # POST /ack must NEVER have been called because database persistence failed
        gateway.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_commit_success_ack_failure_recovery():
    """Scenario 7: Successful database commit followed by ack failure is safely recoverable on redelivery."""
    async with create_isolated_inbound_db() as env:
        gateway = WhatsAppGateway(env["settings"], create_mock_ai_manager())
        gateway.running = True

        message_payload = {
            "message_id": "msg_ack_fail_100",
            "chat_id": "222@s.whatsapp.net",
            "sender": "222",
            "content": "Hello world",
        }

        mock_poll_response = AsyncMock()
        mock_poll_response.raise_for_status = lambda: None
        mock_poll_response.json = lambda: {"messages": [message_payload]}

        # Attempt 1: Poll succeeds, but ACK call to bridge fails with connection error
        mock_post = AsyncMock(side_effect=httpx.ConnectError("Bridge ACK network timeout"))
        gateway.client.get = AsyncMock(return_value=mock_poll_response)
        gateway.client.post = mock_post

        poll_task = asyncio.create_task(gateway._poll_messages())
        await asyncio.sleep(0.08)
        gateway.running = False
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task

        # Verify message WAS durably persisted in SQLite despite ACK failure
        pending = await count_pending_inbound("whatsapp")
        assert pending == 1

        # Attempt 2: Bridge redelivers the same message on next poll
        ok_ack_response = AsyncMock()
        ok_ack_response.raise_for_status = lambda: None
        ok_ack_response.json = lambda: {"status": "acknowledged", "acknowledged_count": 1}
        gateway.client.post = AsyncMock(return_value=ok_ack_response)

        gateway.running = True
        poll_task2 = asyncio.create_task(gateway._poll_messages())
        await asyncio.sleep(0.08)
        gateway.running = False
        poll_task2.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task2

        # Verify: ACK was successfully called with the redelivered ID
        gateway.client.post.assert_called_with(
            f"{gateway.base_url}/ack",
            json={"message_ids": ["msg_ack_fail_100"]},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        # Verify: No duplicate records were created in inbox!
        async with get_session() as session:
            from sqlalchemy import select
            res = await session.execute(select(InboundMessage).where(InboundMessage.external_id == "msg_ack_fail_100"))
            rows = res.scalars().all()
            assert len(rows) == 1


@pytest.mark.asyncio
async def test_duplicate_external_id_suppression():
    """Scenario 8: Redelivering the same external message ID does not create duplicate inbox entries or user messages."""
    async with create_isolated_inbound_db() as env:
        raw_msg = {
            "message_id": "dup_test_id_999",
            "chat_id": "user_dup@s.whatsapp.net",
            "sender": "user_dup",
            "sender_name": "Duplicate User",
            "content": "First ingestion",
        }

        # 1. First ingestion
        persisted1 = await save_inbound_batch([raw_msg])
        assert persisted1 == ["dup_test_id_999"]

        # 2. Duplicate redelivery
        persisted2 = await save_inbound_batch([raw_msg])
        assert persisted2 == ["dup_test_id_999"]  # Returned for ack without error

        # Verify only 1 row exists in inbound_messages
        async with get_session() as session:
            from sqlalchemy import select
            res = await session.execute(select(InboundMessage).where(InboundMessage.external_id == "dup_test_id_999"))
            rows = res.scalars().all()
            assert len(rows) == 1

        # Process the single record
        mock_ai_manager = create_mock_ai_manager()
        mock_ai_manager.match_preset.return_value = "Preset answer"
        gateway = WhatsAppGateway(env["settings"], mock_ai_manager)

        claimed = await claim_next_inbound_message(set())
        assert claimed is not None
        await gateway._process_inbound_record(claimed)

        # Verify only 1 user message and 1 assistant message in conversation history
        conv_messages = await list_messages(1, limit=10)
        assert len(conv_messages) == 2
        assert conv_messages[0].role == "user"
        assert conv_messages[1].role == "assistant"


@pytest.mark.asyncio
async def test_independent_conversations_concurrency():
    """Scenario 9: Messages from independent conversations can be processed concurrently."""
    async with create_isolated_inbound_db() as env:
        # Ingest 2 messages for different chats
        await save_inbound_batch([
            {"message_id": "chatA_msg", "chat_id": "chat_A@s.whatsapp.net", "sender": "user_A", "content": "Hello A"},
            {"message_id": "chatB_msg", "chat_id": "chat_B@s.whatsapp.net", "sender": "user_B", "content": "Hello B"},
        ])

        mock_ai_manager = create_mock_ai_manager()
        gateway = WhatsAppGateway(env["settings"], mock_ai_manager)
        gateway.client.post = AsyncMock()
        gateway.ai_client = AsyncMock()

        # Simulate a 100ms LLM inference duration
        active_inferences = set()
        concurrently_ran = False

        async def slow_inference(prompt):
            nonlocal concurrently_ran
            active_inferences.add(asyncio.current_task())
            if len(active_inferences) >= 2:
                concurrently_ran = True
            await asyncio.sleep(0.08)
            active_inferences.remove(asyncio.current_task())
            return "Concurrent response"

        gateway.ai_client.generate = slow_inference
        gateway.running = True

        # Run 2 workers
        worker1 = asyncio.create_task(gateway._inbox_worker(1))
        worker2 = asyncio.create_task(gateway._inbox_worker(2))
        gateway.work_available_event.set()

        await asyncio.sleep(0.25)
        gateway.running = False
        worker1.cancel()
        worker2.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(worker1, worker2)

        assert concurrently_ran is True, "Independent chats must be processed concurrently"


@pytest.mark.asyncio
async def test_same_conversation_sequential_ordering():
    """Scenario 10: Two messages from the same conversation cannot be processed out of order or simultaneously."""
    async with create_isolated_inbound_db() as env:
        await save_inbound_batch([
            {"message_id": "msg_order_1", "chat_id": "same_chat@s.whatsapp.net", "sender": "user_same", "content": "First"},
            {"message_id": "msg_order_2", "chat_id": "same_chat@s.whatsapp.net", "sender": "user_same", "content": "Second"},
        ])

        mock_ai_manager = create_mock_ai_manager()
        gateway = WhatsAppGateway(env["settings"], mock_ai_manager)
        gateway.client.post = AsyncMock()
        gateway.ai_client = AsyncMock()

        execution_log = []
        active_chat_holders = set()

        async def logging_generate(prompt):
            chat_holder = "same_chat@s.whatsapp.net"
            assert chat_holder not in active_chat_holders, "Collision: Same chat processed concurrently by two workers!"
            active_chat_holders.add(chat_holder)
            is_second = "Second" in prompt
            execution_log.append("start_" + ("2" if is_second else "1"))
            await asyncio.sleep(0.05)
            execution_log.append("end_" + ("2" if is_second else "1"))
            active_chat_holders.remove(chat_holder)
            return "Answer"

        gateway.ai_client.generate = logging_generate
        gateway.running = True

        # Launch 3 concurrent workers
        workers = [asyncio.create_task(gateway._inbox_worker(i)) for i in range(3)]
        gateway.work_available_event.set()

        await asyncio.sleep(0.3)
        gateway.running = False
        for w in workers:
            w.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*workers)

        # Execution must be strictly sequential: start_1 -> end_1 -> start_2 -> end_2
        assert execution_log == ["start_1", "end_1", "start_2", "end_2"], f"Unexpected execution order: {execution_log}"


@pytest.mark.asyncio
async def test_failed_message_does_not_block_worker_pool():
    """Scenario 11: A failed message does not block the worker pool from processing other messages."""
    async with create_isolated_inbound_db() as env:
        env["settings"].inbox_max_attempts = 1
        await save_inbound_batch([
            {"message_id": "msg_will_fail", "chat_id": "fail_chat@s.whatsapp.net", "sender": "fail_user", "content": "Fail me"},
            {"message_id": "msg_will_pass", "chat_id": "pass_chat@s.whatsapp.net", "sender": "pass_user", "content": "Pass me"},
        ])

        mock_ai_manager = create_mock_ai_manager()
        gateway = WhatsAppGateway(env["settings"], mock_ai_manager)
        gateway.client.post = AsyncMock()
        gateway.ai_client = AsyncMock()

        async def selective_inference(prompt):
            if "Fail me" in prompt:
                raise RuntimeError("Unrecoverable inference crash")
            return "Passed response"

        gateway.ai_client.generate = selective_inference
        gateway.running = True

        workers = [asyncio.create_task(gateway._inbox_worker(i)) for i in range(2)]
        gateway.work_available_event.set()

        await asyncio.sleep(0.2)
        gateway.running = False
        for w in workers:
            w.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*workers)

        # Verify: fail message is marked FAILED, and pass message is COMPLETED
        msg_fail = await get_inbound_message(1)
        msg_pass = await get_inbound_message(2)
        assert msg_fail.status == "FAILED"
        assert "RuntimeError" in (msg_fail.last_error or "")
        assert msg_pass.status == "COMPLETED"


@pytest.mark.asyncio
async def test_stale_processing_record_recovery():
    """Scenario 12: A stale processing record with expired lease is recovered on restart."""
    async with create_isolated_inbound_db() as env:
        # Ingest and manually set status to PROCESSING with expired lease
        await save_inbound_batch([
            {"message_id": "stale_msg_1", "chat_id": "stale@s.whatsapp.net", "sender": "stale", "content": "Recover me"}
        ])

        async with get_session() as session:
            from sqlalchemy import update
            await session.execute(
                update(InboundMessage)
                .where(InboundMessage.external_id == "stale_msg_1")
                .values(
                    status="PROCESSING",
                    lease_expires_at=datetime.utcnow() - timedelta(minutes=10),
                )
            )
            await session.commit()

        # Call recover_stale_inbound_leases
        recovered = await recover_stale_inbound_leases(stale_threshold_seconds=60.0)
        assert recovered == 1

        rec = await get_inbound_message(1)
        assert rec.status == "RECEIVED"
        assert rec.lease_expires_at is None


@pytest.mark.asyncio
async def test_worker_concurrency_bounded_limit():
    """Scenario 13: Concurrency never exceeds the configured inbox_worker_count."""
    async with create_isolated_inbound_db() as env:
        env["settings"].inbox_worker_count = 2

        # 6 messages for 6 different chats
        batch = [
            {"message_id": f"concur_{i}", "chat_id": f"chat_{i}@s.whatsapp.net", "sender": f"user_{i}", "content": f"Hi {i}"}
            for i in range(6)
        ]
        await save_inbound_batch(batch)

        mock_ai_manager = create_mock_ai_manager()
        gateway = WhatsAppGateway(env["settings"], mock_ai_manager)
        gateway.client.post = AsyncMock()
        gateway.ai_client = AsyncMock()

        current_concurrency = 0
        max_seen_concurrency = 0

        async def tracked_inference(prompt):
            nonlocal current_concurrency, max_seen_concurrency
            current_concurrency += 1
            if current_concurrency > max_seen_concurrency:
                max_seen_concurrency = current_concurrency
            await asyncio.sleep(0.04)
            current_concurrency -= 1
            return "Tracked answer"

        gateway.ai_client.generate = tracked_inference
        gateway.running = True

        workers = [asyncio.create_task(gateway._inbox_worker(i)) for i in range(2)]
        gateway.work_available_event.set()

        await asyncio.sleep(0.35)
        gateway.running = False
        for w in workers:
            w.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*workers)

        assert max_seen_concurrency == 2, f"Expected peak concurrency of 2, saw: {max_seen_concurrency}"


@pytest.mark.asyncio
async def test_graceful_shutdown_preserves_pending_work():
    """Scenario 14: Graceful shutdown does not erase persisted pending work."""
    async with create_isolated_inbound_db() as env:
        await save_inbound_batch([
            {"message_id": "pending_msg_1", "chat_id": "stay@s.whatsapp.net", "sender": "stay", "content": "Pending"}
        ])

        gateway = WhatsAppGateway(env["settings"], create_mock_ai_manager())
        await gateway.start()
        await asyncio.sleep(0.05)
        await gateway.stop()

        # Verify record remains intact in SQLite
        rec = await get_inbound_message(1)
        assert rec is not None
        assert rec.external_id == "pending_msg_1"
        assert rec.status in ("RECEIVED", "PROCESSING", "COMPLETED")


@pytest.mark.asyncio
async def test_interrupted_response_resumes_without_re_inference():
    """Scenario 15: An interrupted response with RESPONSE_READY status resumes without re-running model inference."""
    async with create_isolated_inbound_db() as env:
        await save_inbound_batch([
            {"message_id": "msg_interrupted_ready", "chat_id": "resume@s.whatsapp.net", "sender": "resume_user", "content": "Need resume"}
        ])

        # Manually simulate interruption: response was generated and persisted, status is RESPONSE_READY
        await mark_inbound_response_ready(1, "Pre-generated durable answer!")

        rec = await get_inbound_message(1)
        assert rec.status == "RESPONSE_READY"
        assert rec.response_text == "Pre-generated durable answer!"

        mock_ai_manager = create_mock_ai_manager()
        gateway = WhatsAppGateway(env["settings"], mock_ai_manager)
        mock_generate = AsyncMock()
        gateway.ai_client.generate = mock_generate

        # Process record
        await gateway._process_inbound_record(rec)

        # Model inference MUST NOT be invoked because response_text was already ready
        mock_generate.assert_not_called()

        # Completed status
        updated_rec = await get_inbound_message(1)
        assert updated_rec.status == "COMPLETED"

        # Message was queued for outgoing delivery
        queued = gateway.outgoing_queue.get_nowait()
        assert queued["text"] == "Pre-generated durable answer!"


def test_node_bridge_queue_suite():
    """Verifies that the Node.js bridge durable queue test suite passes."""
    result = subprocess.run(
        ["node", "whatsapp_bridge/test_bridge_queue.js"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Bridge queue test failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
