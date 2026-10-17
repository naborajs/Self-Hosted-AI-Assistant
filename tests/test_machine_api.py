from __future__ import annotations

import contextlib
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.security import create_access_token, get_password_hash
from config import Settings
from database.connection import init_db, shutdown_db
from database.repository import create_user
from main import create_app


@contextlib.asynccontextmanager
async def create_isolated_test_env():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite+aiosqlite:///{path}"
    test_settings = Settings(
        app_host="127.0.0.1",
        app_port=8000,
        whatsapp_phone="+1234567890",
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        database_url=db_url,
        session_secret="super_secret_session_key_1234567890",
        admin_users="admin_user",
        admin_password_hash=get_password_hash("SecretAdminPass123!"),
        storage_path=tempfile.gettempdir(),
        whatsapp_bridge_secret="bridge_test_secret_999",
        allow_remote_api=False,
        api_keys=SecretStr("service_alpha:secret-alpha-key-999,secret-simple-key"),
        ollama_model="qwen3:8b",
        enable_tool_execution=False,
    )
    await init_db(test_settings)
    try:
        yield {"settings": test_settings, "db_path": path}
    finally:
        await shutdown_db()
        for suffix in ("", "-wal", "-shm"):
            p = f"{path}{suffix}"
            if os.path.exists(p):
                with contextlib.suppress(Exception):
                    os.remove(p)


@pytest.mark.asyncio
async def test_machine_api_authentication():
    async with create_isolated_test_env() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()
            client = TestClient(app)

            # 1. Unauthenticated request -> 401
            resp = client.post("/api/chat", json={"message": "Hello"})
            assert resp.status_code == 401
            assert "Authentication required" in resp.json()["detail"]

            # 2. Invalid API key -> 401
            resp = client.post("/api/chat", json={"message": "Hello"}, headers={"X-API-Key": "wrong-key"})
            assert resp.status_code == 401

            # 3. Invalid Bearer token -> 401
            resp = client.post("/api/chat", json={"message": "Hello"}, headers={"Authorization": "Bearer invalid.token.xyz"})
            assert resp.status_code == 401


@pytest.mark.asyncio
async def test_machine_api_validation():
    async with create_isolated_test_env() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()
            client = TestClient(app)

            headers = {"X-API-Key": "secret-alpha-key-999"}

            # Empty message -> 422
            resp = client.post("/api/chat", json={"message": ""}, headers=headers)
            assert resp.status_code == 422

            # Missing message key -> 422
            resp = client.post("/api/chat", json={"conversation_id": "thread-1"}, headers=headers)
            assert resp.status_code == 422

            # Oversized message (>4096) -> 422
            resp = client.post("/api/chat", json={"message": "x" * 5000}, headers=headers)
            assert resp.status_code == 422


@pytest.mark.asyncio
async def test_machine_api_successful_turn_with_api_key_and_bearer():
    async with create_isolated_test_env() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()

            # Mock OllamaClient
            mock_ollama = MagicMock()
            mock_ollama.generate = AsyncMock(return_value="Greetings from EDITH local AI.")
            mock_ollama.get_installed_models = AsyncMock(return_value=[type("M", (), {"name": "qwen3:8b"})()])
            mock_ollama.is_ollama_available = AsyncMock(return_value=True)
            app.state.ai_client = mock_ollama

            client = TestClient(app)

            # 1. Test using X-API-Key
            resp = client.post(
                "/api/chat",
                json={"message": "Hello assistant", "conversation_id": "thread-101"},
                headers={"X-API-Key": "secret-alpha-key-999"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["response"] == "Greetings from EDITH local AI."
            assert data["conversation_id"] == "thread-101"
            assert data["model_used"] == "qwen3:8b"
            assert data["correlation_id"].startswith("chat-")
            assert "created_at" in data

            # 2. Test using /v1/chat endpoint alias
            resp_v1 = client.post(
                "/v1/chat",
                json={"message": "Hello assistant v1"},
                headers={"X-API-Key": "secret-alpha-key-999"},
            )
            assert resp_v1.status_code == 200
            assert resp_v1.json()["status"] == "success"

            # 3. Test using Bearer Token
            bearer_token = create_access_token(subject="service_beta")
            resp_bearer = client.post(
                "/api/chat",
                json={"message": "Hello via bearer", "conversation_id": "thread-bearer"},
                headers={"Authorization": f"Bearer {bearer_token}"},
            )
            assert resp_bearer.status_code == 200
            assert resp_bearer.json()["response"] == "Greetings from EDITH local AI."


@pytest.mark.asyncio
async def test_machine_api_explicit_model_selection():
    async with create_isolated_test_env() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()

            mock_ollama = MagicMock()
            mock_ollama.generate = AsyncMock(return_value="Output from llama3.")
            mock_ollama.get_installed_models = AsyncMock(return_value=[
                type("M", (), {"name": "qwen3:8b"})(),
                type("M", (), {"name": "llama3:8b"})(),
            ])
            mock_ollama.is_ollama_available = AsyncMock(return_value=True)
            app.state.ai_client = mock_ollama

            client = TestClient(app)
            headers = {"X-API-Key": "secret-alpha-key-999"}

            # Request explicit installed model
            resp = client.post(
                "/api/chat",
                json={"message": "Hi", "model": "llama3:8b"},
                headers=headers,
            )
            assert resp.status_code == 200
            assert resp.json()["model_used"] == "llama3:8b"

            # Request explicit uninstalled model -> 400 Bad Request
            resp_bad = client.post(
                "/api/chat",
                json={"message": "Hi", "model": "mistral-nonexistent:latest"},
                headers=headers,
            )
            assert resp_bad.status_code == 400
            assert "not installed in Ollama" in resp_bad.json()["detail"]


@pytest.mark.asyncio
async def test_machine_api_ollama_offline_error():
    async with create_isolated_test_env() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()

            mock_ollama = MagicMock()
            mock_ollama.get_installed_models = AsyncMock(side_effect=Exception("Connection refused"))
            mock_ollama.is_ollama_available = AsyncMock(return_value=False)
            app.state.ai_client = mock_ollama

            client = TestClient(app)
            headers = {"X-API-Key": "secret-alpha-key-999"}

            resp = client.post(
                "/api/chat",
                json={"message": "Hi"},
                headers=headers,
            )
            assert resp.status_code == 503
            detail = resp.json()["detail"].lower()
            assert "unreachable" in detail or "offline" in detail


@pytest.mark.asyncio
async def test_machine_api_client_isolation():
    async with create_isolated_test_env() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()

            mock_ollama = MagicMock()
            mock_ollama.generate = AsyncMock(return_value="Acknowledged.")
            mock_ollama.get_installed_models = AsyncMock(return_value=[type("M", (), {"name": "qwen3:8b"})()])
            mock_ollama.is_ollama_available = AsyncMock(return_value=True)
            app.state.ai_client = mock_ollama

            client = TestClient(app)

            # Client 1 uses token for "client_one"
            token_client1 = create_access_token(subject="client_one")
            resp1 = client.post(
                "/api/chat",
                json={"message": "Client 1 message", "conversation_id": "thread-main"},
                headers={"Authorization": f"Bearer {token_client1}"},
            )
            assert resp1.status_code == 200

            # Client 2 uses token for "client_two"
            token_client2 = create_access_token(subject="client_two")
            resp2 = client.post(
                "/api/chat",
                json={"message": "Client 2 message", "conversation_id": "thread-main"},
                headers={"Authorization": f"Bearer {token_client2}"},
            )
            assert resp2.status_code == 200

            # Verify distinct users in database
            from database.repository import get_or_create_user, get_conversation
            u1 = await get_or_create_user("api:client_one", "client_one")
            u2 = await get_or_create_user("api:client_two", "client_two")
            assert u1.id != u2.id

            # Verify distinct conversations despite same thread-main name
            c1 = await get_conversation(u1.id, "thread-main")
            c2 = await get_conversation(u2.id, "thread-main")
            assert c1.id != c2.id
            assert c1.user_id == u1.id
            assert c2.user_id == u2.id


@pytest.mark.asyncio
async def test_machine_api_cross_client_memory_and_history_isolation():
    """Verify that Client A cannot read, leak, or overwrite Client B's memories or history."""
    async with create_isolated_test_env() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()

            mock_ollama = MagicMock()
            mock_ollama.generate = AsyncMock(return_value="Understood.")
            mock_ollama.get_installed_models = AsyncMock(return_value=[type("M", (), {"name": "qwen3:8b"})()])
            mock_ollama.is_ollama_available = AsyncMock(return_value=True)
            app.state.ai_client = mock_ollama

            client = TestClient(app)

            # Client A saves a private memory via /remember in thread 'shared-topic'
            token_a = create_access_token(subject="tenant_alpha")
            resp_a = client.post(
                "/api/chat",
                json={"message": "/remember secret_api_pin=987654", "conversation_id": "shared-topic"},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp_a.status_code == 200
            assert "Remembered" in resp_a.json()["response"]

            # Client B asks for memories in the identical conversation_id 'shared-topic'
            token_b = create_access_token(subject="tenant_beta")
            resp_b = client.post(
                "/api/chat",
                json={"message": "/memories", "conversation_id": "shared-topic"},
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp_b.status_code == 200
            # Client B must NOT see Client A's secret memory
            assert "secret_api_pin" not in resp_b.json()["response"]
            assert "987654" not in resp_b.json()["response"]
            assert "You don't have any saved memories yet" in resp_b.json()["response"]

            # Client B attempts to forget Client A's memory
            resp_b_forget = client.post(
                "/api/chat",
                json={"message": "/forget secret_api_pin", "conversation_id": "shared-topic"},
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp_b_forget.status_code == 200
            assert "No memory found" in resp_b_forget.json()["response"]

            # Client A still has the memory intact
            resp_a_check = client.post(
                "/api/chat",
                json={"message": "/memories", "conversation_id": "shared-topic"},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp_a_check.status_code == 200
            assert "secret_api_pin" in resp_a_check.json()["response"]


@pytest.mark.asyncio
async def test_machine_api_unnamed_api_keys_isolation():
    """Verify that multiple raw API keys configured without 'name:' prefixes receive isolated client IDs."""
    from pydantic import SecretStr

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite+aiosqlite:///{path}"
    test_settings = Settings(
        app_host="127.0.0.1",
        app_port=8000,
        database_url=db_url,
        session_secret="test_session_secret_12345678901234567890",
        admin_users="admin_user",
        storage_path=tempfile.gettempdir(),
        api_keys=SecretStr("raw-secret-key-1,raw-secret-key-2"),
        ollama_model="qwen3:8b",
        enable_tool_execution=False,
    )
    await init_db(test_settings)
    try:
        with patch("api.routes.settings", test_settings), patch("api.security.settings", test_settings):
            app = create_app()

            mock_ollama = MagicMock()
            mock_ollama.generate = AsyncMock(return_value="Ack")
            mock_ollama.get_installed_models = AsyncMock(return_value=[type("M", (), {"name": "qwen3:8b"})()])
            mock_ollama.is_ollama_available = AsyncMock(return_value=True)
            app.state.ai_client = mock_ollama

            client = TestClient(app)

            # Key 1 request
            resp1 = client.post(
                "/api/chat",
                json={"message": "Msg 1", "conversation_id": "main"},
                headers={"X-API-Key": "raw-secret-key-1"},
            )
            assert resp1.status_code == 200

            # Key 2 request
            resp2 = client.post(
                "/api/chat",
                json={"message": "Msg 2", "conversation_id": "main"},
                headers={"X-API-Key": "raw-secret-key-2"},
            )
            assert resp2.status_code == 200

            # Verify that two distinct users were created in SQLite
            import hashlib
            from database.repository import get_user_by_external_id
            id1 = f"api:client_{hashlib.sha256(b'raw-secret-key-1').hexdigest()[:8]}"
            id2 = f"api:client_{hashlib.sha256(b'raw-secret-key-2').hexdigest()[:8]}"
            u1 = await get_user_by_external_id(id1)
            u2 = await get_user_by_external_id(id2)
            assert u1 is not None
            assert u2 is not None
            assert u1.id != u2.id
    finally:
        await shutdown_db()
        for suffix in ("", "-wal", "-shm"):
            p = f"{path}{suffix}"
            if os.path.exists(p):
                with contextlib.suppress(Exception):
                    os.remove(p)


@pytest.mark.asyncio
async def test_machine_api_token_security_and_privilege_boundary():
    """Verify token expiry enforcement, invalid subject rejection, and admin endpoint denial."""
    import jwt
    from datetime import datetime, timedelta, timezone

    async with create_isolated_test_env() as env:
        settings = env["settings"]
        with patch("api.routes.settings", settings), patch("api.security.settings", settings):
            app = create_app()
            client = TestClient(app)

            secret_key = settings.session_secret.get_secret_value()

            # 1. Expired token -> 401
            expired_payload = {
                "sub": "client_expired",
                "exp": int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()),
            }
            expired_token = jwt.encode(expired_payload, secret_key, algorithm="HS256")
            resp_exp = client.post(
                "/api/chat",
                json={"message": "Hello"},
                headers={"Authorization": f"Bearer {expired_token}"},
            )
            assert resp_exp.status_code == 401

            # 2. Token with empty sub -> 401
            empty_sub_payload = {
                "sub": "   ",
                "exp": int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()),
            }
            empty_sub_token = jwt.encode(empty_sub_payload, secret_key, algorithm="HS256")
            resp_empty = client.post(
                "/api/chat",
                json={"message": "Hello"},
                headers={"Authorization": f"Bearer {empty_sub_token}"},
            )
            assert resp_empty.status_code == 401

            # 3. Machine API Key cannot access admin routes
            resp_admin = client.get(
                "/api/models",
                headers={"X-API-Key": "secret-alpha-key-999"},
            )
            # Admin endpoints expect Bearer token via OAuth2 scheme; X-API-Key is not valid admin auth
            assert resp_admin.status_code in (401, 403)
