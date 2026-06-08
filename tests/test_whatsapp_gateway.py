import importlib

import pytest
from unittest.mock import AsyncMock


def bootstrap_test_environment(monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE", "+1234567890")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    import config
    importlib.reload(config)
    import whatsapp.service
    importlib.reload(whatsapp.service)
    return config.settings, whatsapp.service.WhatsAppGateway


@pytest.mark.asyncio
async def test_whatsapp_pair_device_calls_bridge(monkeypatch):
    settings, WhatsAppGateway = bootstrap_test_environment(monkeypatch)
    gateway = WhatsAppGateway(settings)
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
    gateway = WhatsAppGateway(settings)
    await gateway.send_message("12345@s.whatsapp.net", "Hello world", "http://example.com/image.png", "image/png", "Caption")
    queued = gateway.outgoing_queue.get_nowait()
    assert queued["chat_id"] == "12345@s.whatsapp.net"
    assert queued["text"] == "Hello world"
    assert queued["file_url"] == "http://example.com/image.png"
    assert queued["mime_type"] == "image/png"
    assert queued["caption"] == "Caption"
