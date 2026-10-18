import asyncio
import contextlib
import importlib
import subprocess
import sys
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient


import os


def bootstrap_test_environment(monkeypatch, bridge_secret: str | None = None):
    monkeypatch.setenv("WHATSAPP_PHONE", "+1234567890")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-32-chars-long!!")
    if bridge_secret is not None:
        monkeypatch.setenv("WHATSAPP_BRIDGE_SECRET", bridge_secret)
    elif "WHATSAPP_BRIDGE_SECRET" not in os.environ:
        monkeypatch.setenv("WHATSAPP_BRIDGE_SECRET", "test-bridge-secret-value-1234")
    import config
    importlib.reload(config)
    import api.security
    importlib.reload(api.security)
    import whatsapp.service
    importlib.reload(whatsapp.service)
    return config.settings, whatsapp.service.WhatsAppGateway


@pytest.mark.asyncio
async def test_whatsapp_pair_device_calls_bridge(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    mock_ai_manager = AsyncMock()
    gateway = WhatsAppGateway(settings, mock_ai_manager)
    gateway.client = AsyncMock()
    response = AsyncMock()
    response.json = lambda: {"pairing_code": "fake-code", "connected": False}
    response.raise_for_status = lambda: None
    gateway.client.post.return_value = response

    result = await gateway.pair_device("+1234567890")

    gateway.client.post.assert_awaited_once()
    assert result["pairing_code"] == "fake-code"
    assert gateway.pairing_code == "fake-code"
    assert gateway.connected is False


@pytest.mark.asyncio
async def test_whatsapp_send_message_queues_payload(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    mock_ai_manager = AsyncMock()
    gateway = WhatsAppGateway(settings, mock_ai_manager)
    await gateway.send_message("12345@s.whatsapp.net", "Hello world", "http://example.com/image.png", "image/png", "Caption")
    queued = gateway.outgoing_queue.get_nowait()
    assert queued["chat_id"] == "12345@s.whatsapp.net"
    assert queued["text"] == "Hello world"
    assert queued["file_url"] == "http://example.com/image.png"
    assert queued["mime_type"] == "image/png"
    assert queued["caption"] == "Caption"


@pytest.mark.asyncio
async def test_whatsapp_gateway_includes_bridge_secret(monkeypatch):
    monkeypatch.setenv("WHATSAPP_BRIDGE_SECRET", "super-bridge-secret-value-1234")
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    mock_ai_manager = AsyncMock()
    gateway = WhatsAppGateway(settings, mock_ai_manager)
    assert gateway.client.headers.get("x-bridge-secret") == "super-bridge-secret-value-1234"


# ============================================================================
# Phase 1B Task 4: Bounded Outgoing WhatsApp Queue & Retry Tests
# ============================================================================

@pytest.mark.asyncio
async def test_process_outgoing_success_first_attempt(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    gateway = WhatsAppGateway(settings, AsyncMock())
    gateway.retry_delay = 0.001
    
    mock_post = AsyncMock()
    ok_response = AsyncMock()
    ok_response.raise_for_status = lambda: None
    mock_post.return_value = ok_response
    gateway.client.post = mock_post

    await gateway.outgoing_queue.put({"chat_id": "1234567890@s.whatsapp.net", "text": "First attempt success"})

    task = asyncio.create_task(gateway._process_outgoing())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert mock_post.call_count == 1
    call_payload = mock_post.call_args[1]["json"]
    assert call_payload["chat_id"] == "1234567890@s.whatsapp.net"
    assert call_payload["text"] == "First attempt success"
    assert "_attempts" not in call_payload
    assert gateway.outgoing_queue.empty()
    assert len(gateway.dead_letter_queue) == 0


@pytest.mark.asyncio
async def test_process_outgoing_transient_failure_then_success(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    gateway = WhatsAppGateway(settings, AsyncMock())
    gateway.retry_delay = 0.001

    ok_response = AsyncMock()
    ok_response.raise_for_status = lambda: None

    mock_post = AsyncMock()
    mock_post.side_effect = [
        httpx.ConnectError("Connection refused by bridge"),
        ok_response,
    ]
    gateway.client.post = mock_post

    await gateway.outgoing_queue.put({"chat_id": "1234567890@s.whatsapp.net", "text": "Retry transient"})

    task = asyncio.create_task(gateway._process_outgoing())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert mock_post.call_count == 2
    assert gateway.outgoing_queue.empty()
    assert len(gateway.dead_letter_queue) == 0


@pytest.mark.asyncio
async def test_process_outgoing_permanent_failure_dead_letters_immediately(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    gateway = WhatsAppGateway(settings, AsyncMock())
    gateway.retry_delay = 0.001

    bad_request_response = httpx.Response(400, request=httpx.Request("POST", "http://test/send-message"))
    mock_post = AsyncMock()
    mock_post.side_effect = httpx.HTTPStatusError("Bad Request", request=bad_request_response.request, response=bad_request_response)
    gateway.client.post = mock_post

    await gateway.outgoing_queue.put({"chat_id": "1234567890@s.whatsapp.net", "text": "Bad request text"})

    task = asyncio.create_task(gateway._process_outgoing())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Permanent failure should NOT retry; exactly 1 attempt
    assert mock_post.call_count == 1
    assert gateway.outgoing_queue.empty()
    assert len(gateway.dead_letter_queue) == 1
    dl = gateway.dead_letter_queue[0]
    assert dl["attempts"] == 1
    assert dl["reason"] == "HTTP 400"
    assert dl["chat_id"] == "1234***@s.whatsapp.net"  # Sanitized recipient!


@pytest.mark.asyncio
async def test_process_outgoing_exhaustion_of_retry_limit(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    gateway = WhatsAppGateway(settings, AsyncMock())
    gateway.retry_delay = 0.001
    gateway.max_send_attempts = 3

    mock_post = AsyncMock()
    mock_post.side_effect = httpx.TimeoutException("Gateway timed out")
    gateway.client.post = mock_post

    await gateway.outgoing_queue.put({"chat_id": "9876543210@s.whatsapp.net", "text": "Timeout text"})

    task = asyncio.create_task(gateway._process_outgoing())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Bounded to exactly 3 attempts
    assert mock_post.call_count == 3
    assert gateway.outgoing_queue.empty()
    assert len(gateway.dead_letter_queue) == 1
    dl = gateway.dead_letter_queue[0]
    assert dl["attempts"] == 3
    assert "TimeoutException" in dl["reason"]
    assert dl["chat_id"] == "9876***@s.whatsapp.net"


@pytest.mark.asyncio
async def test_process_outgoing_next_item_processed_after_earlier_failure(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    gateway = WhatsAppGateway(settings, AsyncMock())
    gateway.retry_delay = 0.001

    ok_response = AsyncMock()
    ok_response.raise_for_status = lambda: None

    bad_request_response = httpx.Response(404, request=httpx.Request("POST", "http://test/send-message"))
    mock_post = AsyncMock()
    mock_post.side_effect = [
        httpx.HTTPStatusError("Not Found", request=bad_request_response.request, response=bad_request_response),
        ok_response,
    ]
    gateway.client.post = mock_post

    await gateway.outgoing_queue.put({"chat_id": "failed@s.whatsapp.net", "text": "Fails"})
    await gateway.outgoing_queue.put({"chat_id": "success@s.whatsapp.net", "text": "Succeeds"})

    task = asyncio.create_task(gateway._process_outgoing())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert mock_post.call_count == 2
    assert gateway.outgoing_queue.empty()
    assert len(gateway.dead_letter_queue) == 1
    assert gateway.dead_letter_queue[0]["chat_id"] == "fail***@s.whatsapp.net"


@pytest.mark.asyncio
async def test_process_outgoing_cancellation_and_clean_shutdown(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    gateway = WhatsAppGateway(settings, AsyncMock())

    task = asyncio.create_task(gateway._process_outgoing())
    await asyncio.sleep(0.01)
    task.cancel()
    # A cancelled task running _process_outgoing should finish cleanly without unhandled exception
    await task
    assert task.done()


# ============================================================================
# Phase 1B Task 6: QR Authentication & Gateway Integration Tests
# ============================================================================

@pytest.mark.asyncio
async def test_whatsapp_gateway_get_qr_success(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    gateway = WhatsAppGateway(settings, AsyncMock())

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.content = b"\x89PNG\r\n\x1a\nfake-png-data"
    gateway.client.get = AsyncMock(return_value=mock_resp)

    qr_bytes = await gateway.get_qr()
    assert qr_bytes == b"\x89PNG\r\n\x1a\nfake-png-data"
    gateway.client.get.assert_awaited_once_with(f"{gateway.base_url}/qr")


@pytest.mark.asyncio
async def test_whatsapp_gateway_get_qr_not_available(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    gateway = WhatsAppGateway(settings, AsyncMock())

    mock_resp = AsyncMock()
    mock_resp.status_code = 404
    gateway.client.get = AsyncMock(return_value=mock_resp)

    qr_bytes = await gateway.get_qr()
    assert qr_bytes is None


@pytest.mark.asyncio
async def test_api_whatsapp_qr_endpoint_requires_auth(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    from main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/whatsapp/qr")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_whatsapp_qr_endpoint_authenticated_flow(monkeypatch):
    import bcrypt
    plain = "TestAdminPass123!"
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashed)
    monkeypatch.setenv("ADMIN_USERS", "admin")

    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    from main import create_app

    app = create_app()
    mock_gateway = AsyncMock()
    mock_gateway.get_qr.return_value = b"\x89PNG\r\n\x1a\nqr-png-binary-stream"
    app.state.whatsapp = mock_gateway

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Get admin token
        token_resp = await client.post("/api/token", data={"username": "admin", "password": plain})
        assert token_resp.status_code == 200
        token = token_resp.json()["access_token"]

        # Call /api/whatsapp/qr
        qr_resp = await client.get("/api/whatsapp/qr", headers={"Authorization": f"Bearer {token}"})
        assert qr_resp.status_code == 200
        assert qr_resp.headers["content-type"] == "image/png"
        assert qr_resp.content == b"\x89PNG\r\n\x1a\nqr-png-binary-stream"

        # When QR is not available, returns 404
        mock_gateway.get_qr.return_value = None
        qr_404 = await client.get("/api/whatsapp/qr", headers={"Authorization": f"Bearer {token}"})
        assert qr_404.status_code == 404


def test_node_bridge_security_suite():
    result = subprocess.run(
        ["node", "whatsapp_bridge/test_bridge_security.js"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Bridge security test failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
