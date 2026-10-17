from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from ai.client import OllamaClient
from ai.manager import AIManager
from ai.processor import execute_conversation_turn
from config import Settings
from database.connection import get_session, init_db, shutdown_db
from database.models import InboundMessage, OutboundMessage, User, Conversation, Message
from database.repository import (
    claim_next_inbound_message,
    count_pending_inbound,
    create_conversation,
    create_or_get_outbound_message,
    get_conversation,
    get_inbound_message,
    get_or_create_user,
    get_outbound_by_inbound_id,
    list_messages,
    mark_inbound_completed,
    mark_inbound_failed,
    mark_inbound_response_ready,
    recover_stale_inbound_leases,
    recover_stale_outbound_leases,
    save_inbound_batch,
    save_message,
)
from main import create_app
from memory.store import MemoryStore
from telegram_gateway.bot import TelegramGateway
from whatsapp.service import WhatsAppGateway


def create_mock_ai_manager(storage_path: str = "") -> AIManager:
    mgr = MagicMock(spec=AIManager)
    mgr.build_system_context.return_value = "System context: You are EDITH."
    mgr.search_knowledge.return_value = []
    mgr.match_preset.return_value = None
    return mgr


def create_mock_model(name: str):
    return type("MockModel", (), {"name": name})()


@contextlib.asynccontextmanager
async def create_isolated_lifecycle_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite+aiosqlite:///{path}"
    test_settings = Settings(
        app_host="127.0.0.1",
        app_port=8000,
        whatsapp_phone="+1234567890",
        telegram_bot_token="your-telegram-bot-token",
        database_url=db_url,
        session_secret="lifecycle_test_session_secret_1234567890",
        admin_users="admin@example.com",
        storage_path=tempfile.gettempdir(),
        whatsapp_bridge_secret="test-bridge-secret",
        allow_remote_api=False,
        api_keys="client_alpha_key,client_beta_key",
        ollama_model="dolphin3:8b",
        inbox_worker_count=2,
        inbox_max_batch_size=50,
        inbox_max_attempts=3,
        inbox_lease_timeout=60.0,
        inbox_poll_interval=0.05,
        inbox_max_pending=200,
    )
    await init_db(test_settings)
    try:
        yield {"settings": test_settings, "db_path": path, "db_url": db_url}
    finally:
        await shutdown_db()
        for ext in ["", "-wal", "-shm"]:
            fpath = path + ext
            if os.path.exists(fpath):
                with contextlib.suppress(OSError):
                    os.remove(fpath)


# ============================================================================
# 1. Telegram Gateway Lifecycle & Resilience Tests
# ============================================================================

@pytest.mark.asyncio
async def test_telegram_gateway_lifecycle_disabled_when_unconfigured():
    """Verify TelegramGateway initializes gracefully with no crash when unconfigured or default placeholder."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        gw = TelegramGateway(settings, MemoryStore(settings), create_mock_ai_manager())
        assert gw.application is None

        # start() and stop() must not raise any exceptions
        await gw.start()
        await gw.stop()


@pytest.mark.asyncio
async def test_telegram_gateway_handle_command_lifecycle():
    """Verify TelegramGateway.handle_command dispatches through execute_conversation_turn and replies."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        memory = MemoryStore(settings)
        ai_mgr = create_mock_ai_manager()
        gw = TelegramGateway(settings, memory, ai_mgr)

        mock_chat = MagicMock()
        mock_chat.id = 987654321
        mock_user = MagicMock()
        mock_user.id = 12345
        mock_user.username = "telegram_operator"
        mock_user.full_name = "Telegram Operator"

        mock_message = AsyncMock()
        mock_message.text = "/remember favorite_language: Python"

        mock_update = MagicMock()
        mock_update.effective_chat = mock_chat
        mock_update.effective_user = mock_user
        mock_update.message = mock_message

        mock_context = MagicMock()

        await gw.handle_command(mock_update, mock_context)

        mock_message.reply_text.assert_called_once()
        reply_content = mock_message.reply_text.call_args[0][0]
        assert "favorite_language" in reply_content or "Saved" in reply_content or "Remembered" in reply_content

        user = await get_or_create_user(f"telegram:{mock_user.id}", "telegram_operator")
        facts = await memory.list_facts(user.id)
        assert any(f.key == "favorite_language" and "Python" in f.value for f in facts)


@pytest.mark.asyncio
async def test_telegram_gateway_handle_message_lifecycle():
    """Verify TelegramGateway.handle_message executes a conversation turn and replies."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        memory = MemoryStore(settings)
        ai_mgr = create_mock_ai_manager()
        gw = TelegramGateway(settings, memory, ai_mgr)

        gw.ai_client = AsyncMock(spec=OllamaClient)
        gw.ai_client.is_ollama_available = AsyncMock(return_value=True)
        gw.ai_client.get_installed_models = AsyncMock(return_value=[create_mock_model(settings.ollama_model)])
        gw.ai_client.generate = AsyncMock(return_value="Greetings, Telegram operator!")

        mock_chat = MagicMock()
        mock_chat.id = 987654321
        mock_user = MagicMock()
        mock_user.id = 12345
        mock_user.username = "telegram_operator"
        mock_user.full_name = "Telegram Operator"

        mock_message = AsyncMock()
        mock_message.text = "Hello EDITH"

        mock_update = MagicMock()
        mock_update.effective_chat = mock_chat
        mock_update.effective_user = mock_user
        mock_update.message = mock_message

        await gw.handle_message(mock_update, MagicMock())

        mock_message.reply_text.assert_called_once()
        reply_sent = mock_message.reply_text.call_args[0][0]
        assert "Greetings, Telegram operator!" in reply_sent

        user = await get_or_create_user(f"telegram:{mock_user.id}", "telegram_operator")
        conv = await get_conversation(user.id, str(mock_chat.id))
        assert conv is not None
        history = await list_messages(conv.id)
        assert len(history) == 2
        assert history[0].role == "user"
        assert history[0].content == "Hello EDITH"
        assert history[1].role == "assistant"
        assert history[1].content == "Greetings, Telegram operator!"


@pytest.mark.asyncio
async def test_telegram_gateway_error_resilience():
    """Verify TelegramGateway catches turn execution errors and gracefully sends error message without crashing."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        gw = TelegramGateway(settings, MemoryStore(settings), create_mock_ai_manager())

        mock_chat = MagicMock()
        mock_chat.id = 987654321
        mock_user = MagicMock()
        mock_user.id = 12345
        mock_user.username = "telegram_operator"
        mock_user.full_name = "Telegram Operator"

        mock_message = AsyncMock()
        mock_message.text = "Crash test"

        mock_update = MagicMock()
        mock_update.effective_chat = mock_chat
        mock_update.effective_user = mock_user
        mock_update.message = mock_message

        with patch("telegram_gateway.bot.execute_conversation_turn", side_effect=RuntimeError("Database I/O error")):
            await gw.handle_message(mock_update, MagicMock())

        mock_message.reply_text.assert_called_once()
        sent_reply = mock_message.reply_text.call_args[0][0]
        assert "An error occurred" in sent_reply


# ============================================================================
# 2. WhatsApp Full Inbound-to-Outbound & Crash Recovery Tests
# ============================================================================

@pytest.mark.asyncio
async def test_whatsapp_full_inbound_outbound_lifecycle():
    """Verify complete WhatsApp lifecycle: batch ingestion -> DB persistence -> worker claim -> turn -> outbound queue."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        ai_mgr = create_mock_ai_manager()
        ai_mgr.match_preset.return_value = "Automated reply to WhatsApp message."

        gw = WhatsAppGateway(settings, ai_mgr)
        gw.client.post = AsyncMock()

        raw_inbound = {
            "message_id": "wa_msg_lifecycle_001",
            "chat_id": "15551234567@s.whatsapp.net",
            "sender": "15551234567",
            "sender_name": "WhatsApp Contact",
            "content": "Ping EDITH",
        }

        # 1. Inbound batch ingestion
        persisted = await save_inbound_batch([raw_inbound], platform="whatsapp")
        assert persisted == ["wa_msg_lifecycle_001"]

        # 2. Worker claims and processes (claimed status is PROCESSING)
        claimed = await claim_next_inbound_message(set(), lease_seconds=60.0, platform="whatsapp")
        assert claimed is not None
        assert claimed.external_id == "wa_msg_lifecycle_001"
        assert claimed.status == "PROCESSING"

        await gw._process_inbound_record(claimed)

        # 3. Inbound message is marked COMPLETED
        updated_inbound = await get_inbound_message(claimed.id)
        assert updated_inbound.status == "COMPLETED"
        assert updated_inbound.response_text == "Automated reply to WhatsApp message."

        # 4. Outbound message created and placed in outgoing_queue
        assert gw.outgoing_queue.qsize() == 1
        queued_item = await gw.outgoing_queue.get()
        assert queued_item["chat_id"] == "15551234567@s.whatsapp.net"
        assert queued_item["text"] == "Automated reply to WhatsApp message."

        # 5. Outbound record exists in SQLite
        outbound = await get_outbound_by_inbound_id(claimed.id)
        assert outbound is not None
        assert outbound.status == "PENDING"
        assert outbound.chat_id == "15551234567@s.whatsapp.net"


@pytest.mark.asyncio
async def test_whatsapp_stale_inbound_lease_recovery_on_startup():
    """Verify recover_stale_inbound_leases restores PROCESSING messages interrupted during a process crash."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        await save_inbound_batch([
            {
                "message_id": "crashed_inbound_msg",
                "chat_id": "15550009999@s.whatsapp.net",
                "sender": "15550009999",
                "content": "Message before crash",
            }
        ])

        claimed = await claim_next_inbound_message(set(), lease_seconds=0.01)
        assert claimed is not None
        assert claimed.status == "PROCESSING"

        await asyncio.sleep(0.02)

        recovered = await recover_stale_inbound_leases(stale_threshold_seconds=0.01)
        assert recovered == 1

        reclaimed = await claim_next_inbound_message(set(), lease_seconds=60.0)
        assert reclaimed is not None
        assert reclaimed.id == claimed.id
        assert reclaimed.status == "PROCESSING"


@pytest.mark.asyncio
async def test_whatsapp_stale_outbound_lease_recovery_on_startup():
    """Verify recover_stale_outbound_leases resets orphaned SENDING outbound messages on restart."""
    async with create_isolated_lifecycle_db() as env:
        outbound = await create_or_get_outbound_message(
            chat_id="15550008888@s.whatsapp.net",
            text="Outbound reply before crash",
        )
        assert outbound.status == "PENDING"

        from database.repository import mark_outbound_sending
        await mark_outbound_sending(outbound.id, lease_seconds=0.01)

        await asyncio.sleep(0.02)

        recovered = await recover_stale_outbound_leases(stale_threshold_seconds=0.01)
        assert recovered == 1

        from database.repository import get_outbound_message
        refreshed = await get_outbound_message(outbound.id)
        assert refreshed.status == "PENDING"
        assert refreshed.chat_id == "15550008888@s.whatsapp.net"


# ============================================================================
# 3. Machine Chat API Diagnostic Resilience Tests
# ============================================================================

@pytest.mark.asyncio
async def test_machine_api_diagnostic_error_when_model_uninstalled():
    """Verify Machine Chat API returns 400 Bad Request with available model list when uninstalled model requested."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()

            mock_client = MagicMock()
            mock_client.is_ollama_available = AsyncMock(return_value=True)
            mock_client.get_installed_models = AsyncMock(return_value=[
                create_mock_model("qwen3:8b"),
                create_mock_model("mistral:latest"),
            ])
            app.state.ai_client = mock_client
            app.state.ai_manager.match_preset = MagicMock(return_value=None)

            client = TestClient(app)
            response = client.post(
                "/api/chat",
                json={
                    "message": "Analyze system performance",
                    "model": "nonexistent-model-xyz",
                },
                headers={"X-API-Key": "client_alpha_key"},
            )
            assert response.status_code == 400
            data = response.json()
            assert "Requested model 'nonexistent-model-xyz' is not installed" in data["detail"]
            assert "qwen3:8b" in data["detail"]


@pytest.mark.asyncio
async def test_machine_api_diagnostic_error_when_ollama_unreachable():
    """Verify Machine Chat API returns 503 Service Unavailable when Ollama is unreachable."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()

            mock_client = MagicMock()
            mock_client.is_ollama_available = AsyncMock(return_value=False)
            mock_client.get_installed_models = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            app.state.ai_client = mock_client
            app.state.ai_manager.match_preset = MagicMock(return_value=None)

            client = TestClient(app)
            response = client.post(
                "/api/chat",
                json={"message": "Analyze system performance"},
                headers={"X-API-Key": "client_alpha_key"},
            )
            assert response.status_code == 503
            data = response.json()
            assert "Ollama" in data["detail"] or "Inference service unavailable" in data["detail"]


# ============================================================================
# 4. Concurrency, Serialization & Thread Isolation Across Channels
# ============================================================================

@pytest.mark.asyncio
async def test_concurrency_multi_chat_simultaneous_processing():
    """Verify simultaneous conversation turns across distinct channels run concurrently without data corruption."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        ai_mgr = create_mock_ai_manager()
        ai_client = AsyncMock(spec=OllamaClient)
        ai_client.is_ollama_available = AsyncMock(return_value=True)
        ai_client.get_installed_models = AsyncMock(return_value=[create_mock_model(settings.ollama_model)])

        active_turns = 0
        max_concurrent_turns = 0
        lock = asyncio.Lock()

        async def concurrent_generate(*args, **kwargs):
            nonlocal active_turns, max_concurrent_turns
            async with lock:
                active_turns += 1
                if active_turns > max_concurrent_turns:
                    max_concurrent_turns = active_turns
            await asyncio.sleep(0.05)
            async with lock:
                active_turns -= 1
            return "Simulated concurrent reply"

        ai_client.generate = concurrent_generate

        results = await asyncio.gather(
            execute_conversation_turn(
                external_id="whatsapp:+15551111",
                sender_name="Alice",
                chat_id="alice_chat",
                content="WhatsApp question",
                platform="whatsapp",
                ai_client=ai_client,
                ai_manager=ai_mgr,
                app_settings=settings,
            ),
            execute_conversation_turn(
                external_id="telegram:22222",
                sender_name="Bob",
                chat_id="bob_chat",
                content="Telegram question",
                platform="telegram",
                ai_client=ai_client,
                ai_manager=ai_mgr,
                app_settings=settings,
            ),
            execute_conversation_turn(
                external_id="api:client_alpha",
                sender_name="client_alpha",
                chat_id="api_thread_1",
                content="API question",
                platform="api",
                ai_client=ai_client,
                ai_manager=ai_mgr,
                app_settings=settings,
            ),
        )

        assert len(results) == 3
        for turn in results:
            assert turn.response == "Simulated concurrent reply"
            assert not turn.is_error

        assert max_concurrent_turns >= 2


@pytest.mark.asyncio
async def test_concurrency_same_conversation_serialization():
    """Verify that multiple messages for the exact same chat ID are serialized to prevent race conditions."""
    async with create_isolated_lifecycle_db() as env:
        await save_inbound_batch([
            {
                "message_id": "same_chat_1",
                "chat_id": "same_chat@s.whatsapp.net",
                "sender": "user_same",
                "content": "First message",
            },
            {
                "message_id": "same_chat_2",
                "chat_id": "same_chat@s.whatsapp.net",
                "sender": "user_same",
                "content": "Second message",
            },
        ])

        active_chats = set()

        claimed1 = await claim_next_inbound_message(active_chats, lease_seconds=60.0)
        assert claimed1 is not None
        assert claimed1.external_id == "same_chat_1"

        active_chats.add(claimed1.chat_id)

        claimed2 = await claim_next_inbound_message(active_chats, lease_seconds=60.0)
        assert claimed2 is None

        active_chats.discard(claimed1.chat_id)
        await mark_inbound_completed(claimed1.id)

        claimed2_after = await claim_next_inbound_message(active_chats, lease_seconds=60.0)
        assert claimed2_after is not None
        assert claimed2_after.external_id == "same_chat_2"


@pytest.mark.asyncio
async def test_concurrency_slow_turn_does_not_block_independent_conversation():
    """Verify a long-running turn in conversation A does not block or delay conversation B."""
    async with create_isolated_lifecycle_db() as env:
        settings = env["settings"]
        ai_mgr = create_mock_ai_manager()
        ai_client = AsyncMock(spec=OllamaClient)
        ai_client.is_ollama_available = AsyncMock(return_value=True)
        ai_client.get_installed_models = AsyncMock(return_value=[create_mock_model(settings.ollama_model)])

        async def variable_speed_generate(prompt, *args, **kwargs):
            if "slow" in prompt.lower():
                await asyncio.sleep(0.12)
                return "Slow response"
            else:
                return "Fast response"

        ai_client.generate = variable_speed_generate

        t_start = asyncio.get_event_loop().time()

        task_slow = asyncio.create_task(
            execute_conversation_turn(
                external_id="whatsapp:+15559991",
                sender_name="SlowUser",
                chat_id="slow_chat",
                content="Please run a slow turn",
                platform="whatsapp",
                ai_client=ai_client,
                ai_manager=ai_mgr,
                app_settings=settings,
            )
        )

        await asyncio.sleep(0.01)

        result_fast = await execute_conversation_turn(
            external_id="whatsapp:+15559992",
            sender_name="FastUser",
            chat_id="fast_chat",
            content="Fast turn",
            platform="whatsapp",
            ai_client=ai_client,
            ai_manager=ai_mgr,
            app_settings=settings,
        )
        t_fast_done = asyncio.get_event_loop().time()

        elapsed_fast = t_fast_done - t_start
        assert elapsed_fast < 0.10
        assert result_fast.response == "Fast response"

        result_slow = await task_slow
        assert result_slow.response == "Slow response"
