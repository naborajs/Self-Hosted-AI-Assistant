from __future__ import annotations

import contextlib
import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.security import create_access_token, get_password_hash
from config import Settings
from database.connection import init_db, shutdown_db
from database.models import User
from database.repository import (
    create_or_get_outbound_message,
    create_user,
    mark_outbound_failed,
    mark_outbound_sent,
)
from main import create_app


@contextlib.asynccontextmanager
async def create_isolated_test_db():
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
        admin_users="admin_user,superadmin",
        admin_password_hash=get_password_hash("SecretAdminPass123!"),
        storage_path=tempfile.gettempdir(),
        whatsapp_bridge_secret="bridge_test_secret_999",
        allow_remote_api=False,
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
async def test_admin_overview_endpoint_security_and_content():
    async with create_isolated_test_db() as env:
        settings = env["settings"]
        import api.routes
        with patch.object(api.routes, "settings", settings):
            app = create_app()
            client = TestClient(app)

            # 1. Unauthenticated request -> 401
            resp = client.get("/api/admin/overview")
            assert resp.status_code == 401

            # 2. Non-admin authenticated request -> 403
            non_admin_token = create_access_token(subject="regular_user")
            resp = client.get("/api/admin/overview", headers={"Authorization": f"Bearer {non_admin_token}"})
            assert resp.status_code == 403

            # 3. Admin authenticated request -> 200
            admin_token = create_access_token(subject="admin_user")
            resp = client.get("/api/admin/overview", headers={"Authorization": f"Bearer {admin_token}"})
            assert resp.status_code == 200
            data = resp.json()

            assert "status" in data
            assert "system" in data
            assert data["system"]["loopback_only"] is True
            assert data["system"]["remote_api_allowed"] is False
            assert "dependencies" in data
            assert "queues" in data
            assert "tools_count" in data
            assert "warnings" in data


@pytest.mark.asyncio
async def test_admin_users_endpoints_with_masking():
    async with create_isolated_test_db() as env:
        settings = env["settings"]
        import api.routes
        with patch.object(api.routes, "settings", settings):
            app = create_app()
            client = TestClient(app)

            # Pre-seed users
            await create_user("whatsapp:1234567890@s.whatsapp.net", "Admin User", is_admin=True)
            await create_user("telegram:987654321", "Regular User", is_admin=False)

            admin_token = create_access_token(subject="admin_user")

            # List users
            resp = client.get("/api/admin/users", headers={"Authorization": f"Bearer {admin_token}"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["count"] >= 2
            users = data["users"]

            # Verify sensitive external_id is masked
            for u in users:
                assert "***" in u["external_id"]
                assert "@" in u["external_id"] or ":" in u["external_id"]

            # Show single user
            user_id = users[0]["id"]
            resp = client.get(f"/api/admin/users/{user_id}", headers={"Authorization": f"Bearer {admin_token}"})
            assert resp.status_code == 200
            detail = resp.json()
            assert detail["id"] == user_id
            assert "***" in detail["external_id"]

            # Non-existent user -> 404
            resp = client.get("/api/admin/users/99999", headers={"Authorization": f"Bearer {admin_token}"})
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_tools_catalog_and_policy():
    async with create_isolated_test_db() as env:
        settings = env["settings"]
        import api.routes
        with patch.object(api.routes, "settings", settings):
            app = create_app()
            client = TestClient(app)
            admin_token = create_access_token(subject="admin_user")

            resp = client.get("/api/admin/tools", headers={"Authorization": f"Bearer {admin_token}"})
            assert resp.status_code == 200
            data = resp.json()

            assert "tools" in data
            tool_names = [t["name"] for t in data["tools"]]
            assert "calculator" in tool_names
            assert "search" in tool_names
            assert "file_manager_write" in tool_names

            policy = data["policy"]
            assert policy["enable_tool_execution"] is True
            assert policy["allow_external_network_tools"] is False


@pytest.mark.asyncio
async def test_admin_integrations_status_and_masked_secrets():
    async with create_isolated_test_db() as env:
        settings = env["settings"]
        import api.routes
        with patch.object(api.routes, "settings", settings):
            app = create_app()
            client = TestClient(app)
            admin_token = create_access_token(subject="admin_user")

            resp = client.get("/api/admin/integrations", headers={"Authorization": f"Bearer {admin_token}"})
            assert resp.status_code == 200
            data = resp.json()

            # WhatsApp
            wa = data["whatsapp"]
            assert wa["configured"] is True
            assert "***" in wa["configured_phone_masked"]
            assert wa["has_bridge_secret"] is True
            assert "bridge_url" in wa

            # Telegram
            tg = data["telegram"]
            assert tg["configured"] is True
            assert "***" in tg["token_masked"]
            assert "ABC-DEF" not in tg["token_masked"]  # Ensure secret token portion is masked!


@pytest.mark.asyncio
async def test_admin_diagnostics_deliveries_and_retry():
    async with create_isolated_test_db() as env:
        settings = env["settings"]
        import api.routes
        with patch.object(api.routes, "settings", settings):
            app = create_app()
            client = TestClient(app)
            admin_token = create_access_token(subject="admin_user")

            # Create dummy outbound messages: one sent, one failed
            msg_sent = await create_or_get_outbound_message(
                chat_id="1234567890@s.whatsapp.net",
                text="Sent delivery message",
                correlation_id="corr_sent_100",
            )
            await mark_outbound_sent(msg_sent.id)

            msg_failed = await create_or_get_outbound_message(
                chat_id="9876543210@s.whatsapp.net",
                text="Failed delivery message",
                correlation_id="corr_failed_101",
            )
            await mark_outbound_failed(msg_failed.id, "Simulated network timeout")

            # Query diagnostics
            resp = client.get("/api/admin/diagnostics/deliveries", headers={"Authorization": f"Bearer {admin_token}"})
            assert resp.status_code == 200
            diag = resp.json()

            outbound = diag["outbound"]
            assert len(outbound) >= 2
            # Check that chat_id is masked
            for o in outbound:
                assert "***" in o["chat_id"]

            # Retry the failed outbound message
            retry_resp = client.post(
                f"/api/admin/diagnostics/outbound/{msg_failed.id}/retry",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert retry_resp.status_code == 200
            retry_data = retry_resp.json()
            assert retry_data["status"] == "requeued"
            assert retry_data["message"]["status"] == "PENDING"

            # Attempting to retry an already SENT message -> 400
            retry_sent_resp = client.post(
                f"/api/admin/diagnostics/outbound/{msg_sent.id}/retry",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert retry_sent_resp.status_code == 400
