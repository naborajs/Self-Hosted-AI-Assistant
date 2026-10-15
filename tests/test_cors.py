from __future__ import annotations

import importlib
import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
async def test_cors_trusted_origin(monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE", "+1234567890")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("SESSION_SECRET", "super-secret-jwt-key-minimum-32-chars-long!")
    monkeypatch.setenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:3000")

    import config
    importlib.reload(config)
    from main import create_app
    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Preflight OPTIONS request from trusted origin
        response = await client.options(
            "/api/token",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert "access-control-allow-credentials" in response.headers
        assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.asyncio
async def test_cors_untrusted_origin_rejected(monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE", "+1234567890")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("SESSION_SECRET", "super-secret-jwt-key-minimum-32-chars-long!")
    monkeypatch.setenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:3000")

    import config
    importlib.reload(config)
    from main import create_app
    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Request from malicious/untrusted origin
        response = await client.options(
            "/api/token",
            headers={
                "Origin": "http://malicious-site.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        # Untrusted origin must NOT be granted access-control-allow-origin
        assert response.headers.get("access-control-allow-origin") != "http://malicious-site.example.com"
        assert response.headers.get("access-control-allow-origin") != "*"
