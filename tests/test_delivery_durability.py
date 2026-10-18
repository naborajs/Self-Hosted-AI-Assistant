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
from sqlalchemy import select

from ai.manager import AIManager
from database.connection import get_session, init_db, shutdown_db
from database.models import Conversation, InboundMessage, Message, OutboundMessage, User
from database.repository import (
    claim_next_outbound_message,
    count_pending_outbound,
    create_conversation,
    create_or_get_outbound_message,
    get_inbound_message,
    get_or_create_user,
    get_outbound_by_inbound_id,
    get_outbound_message,
    list_messages,
    mark_inbound_completed,
    mark_inbound_response_ready,
    mark_outbound_ambiguous,
    mark_outbound_failed,
    mark_outbound_sending,
    mark_outbound_sent,
    recover_stale_inbound_leases,
    recover_stale_outbound_leases,
    requeue_outbound_retry,
    save_inbound_batch,
    save_message,
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
        self.inbox_worker_count = 2
        self.inbox_max_batch_size = 50
        self.inbox_max_attempts = 3
        self.inbox_lease_timeout = 60.0
        self.inbox_poll_interval = 0.05
        self.inbox_max_pending = 100


@contextlib.asynccontextmanager
async def create_isolated_durability_db():
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
# Phase 2.1 Test Suite: Delivery Durability and Outbound Recovery
# ============================================================================

@pytest.mark.asyncio
async def test_outbound_delivery_recovery_without_re_inference():
    """Scenario 1: Outbound delivery recovery after process crash reconstructs
    pending deliveries from SQLite and dispatches without re-running LLM inference."""
    async with create_isolated_durability_db() as env:
        # Pre-seed a pending outbound response representing work interrupted before dispatch
        outbound = await create_or_get_outbound_message(
            chat_id="recov_user@s.whatsapp.net",
            text="Durable answer saved before crash",
            platform="whatsapp",
            correlation_id="corr-crash-1",
        )
        assert outbound.status == "PENDING"
        assert outbound.attempt_count == 0

        mock_ai_manager = create_mock_ai_manager()
        gateway = WhatsAppGateway(env["settings"], mock_ai_manager)
        gateway.ai_client = AsyncMock()

        # Mock bridge health and send endpoints
        mock_post_resp = AsyncMock()
        mock_post_resp.status_code = 200
        mock_post_resp.raise_for_status = lambda: None
        mock_post_resp.json = lambda: {"success": True}

        gateway.client.get = AsyncMock(return_value=AsyncMock(status_code=200, json=lambda: {"connected": True}))
        gateway.client.post = AsyncMock(return_value=mock_post_resp)

        # Start gateway (reconstructs pending outbounds from SQLite)
        await gateway.start()
        await asyncio.sleep(0.1)
        await gateway.stop()

        # Verify inference was NOT invoked
        gateway.ai_client.generate.assert_not_called()

        # Verify outbound message transitioned to SENT in SQLite
        updated_outbound = await get_outbound_message(outbound.id)
        assert updated_outbound is not None
        assert updated_outbound.status == "SENT"
        assert updated_outbound.sent_at is not None
        assert updated_outbound.attempt_count == 1


@pytest.mark.asyncio
async def test_prevention_of_duplicate_assistant_history_on_recovery():
    """Scenario 2: Resuming an interrupted inbound record prevents duplicate
    assistant history records when assistant message was already created."""
    async with create_isolated_durability_db() as env:
        # 1. Create user and conversation
        user = await get_or_create_user("whatsapp:+19998887777", "TestUser")
        conv = await create_conversation(user.id, "dupe_test@s.whatsapp.net", title="dupe_test@s.whatsapp.net")
        conv_id = conv.id

        # 2. Ingest inbound message
        await save_inbound_batch([
            {
                "message_id": "inbound_dupe_check_1",
                "chat_id": "dupe_test@s.whatsapp.net",
                "sender": "+19998887777",
                "content": "Tell me a joke",
            }
        ])
        inbound = await get_inbound_message(1)
        assert inbound is not None

        # 3. Simulate stage 1 completion: response generated
        await mark_inbound_response_ready(inbound.id, "Why did the chicken cross the road?")

        # 4. Simulate crash right after saving assistant message in history and creating OutboundMessage
        msg = await save_message(
            conv_id,
            "assistant",
            "assistant",
            "Why did the chicken cross the road?",
        )
        outbound = await create_or_get_outbound_message(
            chat_id="dupe_test@s.whatsapp.net",
            text="Why did the chicken cross the road?",
            inbound_id=inbound.id,
            conversation_id=conv_id,
            message_id=msg.id,
            correlation_id=f"inbound-{inbound.id}",
        )
        assert outbound.message_id == msg.id

        # 5. Now recover and process the inbound record again
        mock_ai_manager = create_mock_ai_manager()
        gateway = WhatsAppGateway(env["settings"], mock_ai_manager)
        gateway.ai_client = AsyncMock()

        # Inbound record is re-processed by worker
        recovered_inbound = await get_inbound_message(inbound.id)
        await gateway._process_inbound_record(recovered_inbound)

        # 6. Verify LLM generate was NOT called
        gateway.ai_client.generate.assert_not_called()

        # 7. Check message history in SQLite: MUST HAVE EXACTLY 1 assistant message, NOT 2!
        messages = await list_messages(conv_id, limit=10)
        assistant_messages = [m for m in messages if m.role == "assistant"]
        assert len(assistant_messages) == 1, f"Expected exactly 1 assistant message, found {len(assistant_messages)}"
        assert assistant_messages[0].id == msg.id


@pytest.mark.asyncio
async def test_outbound_confirmed_delivery_updates_sent():
    """Scenario 3: Confirmed delivery via bridge HTTP 200 marks OutboundMessage
    as SENT with a valid timestamp."""
    async with create_isolated_durability_db() as env:
        gateway = WhatsAppGateway(env["settings"], create_mock_ai_manager())
        gateway.running = True

        mock_post_resp = AsyncMock()
        mock_post_resp.status_code = 200
        mock_post_resp.raise_for_status = lambda: None
        gateway.client.post = AsyncMock(return_value=mock_post_resp)

        outbound = await gateway.send_message(
            chat_id="confirm_user@s.whatsapp.net",
            text="Hello from delivery confirmation test",
        )
        assert outbound is not None
        assert outbound.status == "PENDING"

        # Let the outgoing worker process the item
        worker_task = asyncio.create_task(gateway._process_outgoing())
        await asyncio.sleep(0.08)
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

        # Verify DB status
        rec = await get_outbound_message(outbound.id)
        assert rec is not None
        assert rec.status == "SENT"
        assert rec.sent_at is not None
        assert rec.attempt_count == 1
        assert rec.last_error is None


@pytest.mark.asyncio
async def test_outbound_read_timeout_transitions_to_ambiguous():
    """Scenario 4: A ReadTimeout during send transitions OutboundMessage to AMBIGUOUS,
    dead-letters it, and halts blind duplicate sending."""
    async with create_isolated_durability_db() as env:
        gateway = WhatsAppGateway(env["settings"], create_mock_ai_manager())
        gateway.running = True

        # Mock ReadTimeout (request sent over the wire, but read timed out)
        req = httpx.Request("POST", "http://127.0.0.1:3000/send-message")
        gateway.client.post = AsyncMock(side_effect=httpx.ReadTimeout("Read timed out waiting for bridge", request=req))

        outbound = await gateway.send_message(
            chat_id="ambiguous_user@s.whatsapp.net",
            text="Did this deliver? Outcome is ambiguous.",
        )
        assert outbound is not None

        worker_task = asyncio.create_task(gateway._process_outgoing())
        await asyncio.sleep(0.08)
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

        # Verify DB status is AMBIGUOUS (not retried)
        rec = await get_outbound_message(outbound.id)
        assert rec is not None
        assert rec.status == "AMBIGUOUS"
        assert "ReadTimeout" in (rec.last_error or "")
        assert rec.lease_expires_at is None

        # Verify dead letter entry exists
        assert len(gateway.dead_letter_queue) == 1
        dlq_entry = gateway.dead_letter_queue[0]
        assert "Ambiguous" in dlq_entry["reason"]

        # Verify outgoing queue is empty (blind retry halted)
        assert gateway.outgoing_queue.empty()


@pytest.mark.asyncio
async def test_outbound_permanent_4xx_transitions_to_failed():
    """Scenario 5: 4xx Client error (e.g. 400 Bad Request) immediately dead-letters
    to FAILED without infinite retry."""
    async with create_isolated_durability_db() as env:
        gateway = WhatsAppGateway(env["settings"], create_mock_ai_manager())
        gateway.running = True

        req = httpx.Request("POST", "http://127.0.0.1:3000/send-message")
        resp = httpx.Response(400, request=req, text="Invalid chat JID format")
        gateway.client.post = AsyncMock(side_effect=httpx.HTTPStatusError("Client Error", request=req, response=resp))

        outbound = await gateway.send_message(
            chat_id="invalid_user_jid",
            text="Invalid JID test message",
        )
        assert outbound is not None

        worker_task = asyncio.create_task(gateway._process_outgoing())
        await asyncio.sleep(0.08)
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

        # Verify DB status is permanently FAILED
        rec = await get_outbound_message(outbound.id)
        assert rec is not None
        assert rec.status == "FAILED"
        assert "HTTP 400" in (rec.last_error or "")
        assert rec.attempt_count == 1

        # In-memory queue empty, dead-letter recorded
        assert gateway.outgoing_queue.empty()
        assert len(gateway.dead_letter_queue) == 1
        assert gateway.dead_letter_queue[0]["reason"] == "HTTP 400"


@pytest.mark.asyncio
async def test_outbound_transient_5xx_retries_then_fails():
    """Scenario 6: 5xx server error retries up to max_attempts then transitions to FAILED."""
    async with create_isolated_durability_db() as env:
        gateway = WhatsAppGateway(env["settings"], create_mock_ai_manager())
        gateway.running = True
        gateway.retry_delay = 0.01  # accelerate retry delay for testing
        gateway.max_send_attempts = 3

        req = httpx.Request("POST", "http://127.0.0.1:3000/send-message")
        resp = httpx.Response(503, request=req, text="Bridge Service Unavailable")
        gateway.client.post = AsyncMock(side_effect=httpx.HTTPStatusError("Service Unavailable", request=req, response=resp))

        outbound = await gateway.send_message(
            chat_id="transient_user@s.whatsapp.net",
            text="Transient failure test",
        )
        assert outbound is not None

        worker_task = asyncio.create_task(gateway._process_outgoing())
        await asyncio.sleep(0.15)
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

        # Verify DB status reached FAILED after exhausting max attempts
        rec = await get_outbound_message(outbound.id)
        assert rec is not None
        assert rec.status == "FAILED"
        assert rec.attempt_count == 3
        assert "503" in (rec.last_error or "")

        # Verify dead letter recorded
        assert len(gateway.dead_letter_queue) == 1
        assert gateway.dead_letter_queue[0]["attempts"] == 3


@pytest.mark.asyncio
async def test_stale_outbound_lease_recovery():
    """Scenario 7: Stale outbound messages stuck in SENDING have their leases recovered.
    Messages under max attempts revert to PENDING; messages at max attempts fail."""
    async with create_isolated_durability_db() as env:
        now = datetime.utcnow()
        expired = now - timedelta(seconds=120)

        # 1. Message under max attempts (attempt 1/3)
        msg1 = await create_or_get_outbound_message(
            chat_id="stale1@s.whatsapp.net",
            text="Stale attempt 1",
            correlation_id="stale-corr-1",
        )
        # 2. Message at max attempts (attempt 3/3)
        msg2 = await create_or_get_outbound_message(
            chat_id="stale2@s.whatsapp.net",
            text="Stale attempt 3",
            correlation_id="stale-corr-2",
        )

        async with get_session() as session:
            # Force them into SENDING with expired lease
            from sqlalchemy import update
            await session.execute(
                update(OutboundMessage)
                .where(OutboundMessage.id == msg1.id)
                .values(status="SENDING", attempt_count=1, lease_expires_at=expired)
            )
            await session.execute(
                update(OutboundMessage)
                .where(OutboundMessage.id == msg2.id)
                .values(status="SENDING", attempt_count=3, lease_expires_at=expired)
            )
            await session.commit()

        # Run lease recovery
        recovered_count = await recover_stale_outbound_leases(stale_threshold_seconds=60.0)
        assert recovered_count == 2

        # Check msg1 -> reverted to PENDING
        rec1 = await get_outbound_message(msg1.id)
        assert rec1.status == "PENDING"
        assert rec1.lease_expires_at is None

        # Check msg2 -> transitioned to FAILED
        rec2 = await get_outbound_message(msg2.id)
        assert rec2.status == "FAILED"
        assert rec2.lease_expires_at is None


@pytest.mark.asyncio
async def test_graceful_shutdown_preserves_pending_outbound():
    """Scenario 8: Graceful shutdown preserves pending outbound records in SQLite."""
    async with create_isolated_durability_db() as env:
        outbound = await create_or_get_outbound_message(
            chat_id="shutdown_test@s.whatsapp.net",
            text="Pending outbound record to be preserved",
            correlation_id="corr-shutdown-1",
        )
        assert outbound.status == "PENDING"

        gateway = WhatsAppGateway(env["settings"], create_mock_ai_manager())
        gateway.client.get = AsyncMock(return_value=AsyncMock(status_code=200, json=lambda: {"connected": True}))

        # Simulate network latency so dispatch is in-flight when shutdown occurs
        async def delayed_post(*args, **kwargs):
            await asyncio.sleep(5.0)
            mock_res = AsyncMock()
            mock_res.status_code = 200
            return mock_res

        gateway.client.post = delayed_post

        await gateway.start()
        await asyncio.sleep(0.05)
        await gateway.stop()

        # Verify record remains intact in SQLite and was not erased or dropped
        rec = await get_outbound_message(outbound.id)
        assert rec is not None
        assert rec.text == "Pending outbound record to be preserved"
        assert rec.status in ("PENDING", "SENDING")

        # After shutdown, recovering stale leases restores it cleanly to PENDING for subsequent restart
        await recover_stale_outbound_leases(stale_threshold_seconds=0.0)
        rec_after = await get_outbound_message(outbound.id)
        assert rec_after.status == "PENDING"


def test_bridge_storage_deterministic_path():
    """Scenario 9: Verify Node.js bridge queue storage uses deterministic absolute path
    and supports environment variable override."""
    test_script = """
    const path = require('path');
    const bridge = require('./whatsapp_bridge/index.js');
    
    // Check default storage path is defined and absolute
    if (!bridge.STORAGE_PATH) {
        console.error('STORAGE_PATH not exported');
        process.exit(1);
    }
    if (!path.isAbsolute(bridge.STORAGE_PATH)) {
        console.error('STORAGE_PATH is not absolute:', bridge.STORAGE_PATH);
        process.exit(1);
    }
    
    console.log('Deterministic storage path verified:', bridge.STORAGE_PATH);
    process.exit(0);
    """
    result = subprocess.run(
        ["node", "-e", test_script],
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Bridge path test failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    # Also test WHATSAPP_STORAGE_DIR override in isolated child process
    override_script = """
    const path = require('path');
    process.env.WHATSAPP_STORAGE_DIR = '/custom/test/storage';
    delete require.cache[require.resolve('./whatsapp_bridge/index.js')];
    const bridge = require('./whatsapp_bridge/index.js');
    if (bridge.STORAGE_PATH !== path.resolve('/custom/test/storage')) {
        console.error('Mismatch: expected /custom/test/storage, got ' + bridge.STORAGE_PATH);
        process.exit(1);
    }
    process.exit(0);
    """
    res_override = subprocess.run(
        ["node", "-e", override_script],
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
    )
    assert res_override.returncode == 0, f"Override test failed:\nstdout: {res_override.stdout}\nstderr: {res_override.stderr}"
