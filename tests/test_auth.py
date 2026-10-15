from __future__ import annotations

import importlib
import pytest
from httpx import AsyncClient, ASGITransport
from pydantic import SecretStr

import bcrypt
import jwt


def bootstrap_auth_env(monkeypatch, password_hash: str | None = None, admin_users: str = "admin_user"):
    monkeypatch.setenv("WHATSAPP_PHONE", "+1234567890")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("SESSION_SECRET", "super-secret-jwt-key-minimum-32-chars-long!")
    monkeypatch.setenv("ADMIN_USERS", admin_users)
    if password_hash:
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", password_hash)
    else:
        monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)

    import config
    importlib.reload(config)
    import api.security
    importlib.reload(api.security)
    return config.settings, api.security


def test_password_hashing_and_verification():
    import api.security
    plain = "SuperSecurePassword123!"
    hashed = api.security.get_password_hash(plain)
    assert hashed.startswith("$2b$") or hashed.startswith("$2a$")
    assert api.security.verify_password(plain, hashed) is True
    assert api.security.verify_password("WrongPassword!", hashed) is False
    assert api.security.verify_password("", hashed) is False
    assert api.security.verify_password(plain, "") is False


def test_authenticate_admin_success(monkeypatch):
    plain = "CorrectAdminPassword99!"
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    settings, sec = bootstrap_auth_env(monkeypatch, password_hash=hashed, admin_users="chief_admin")

    user = sec.authenticate_admin("chief_admin", plain)
    assert user is not None
    assert user["username"] == "chief_admin"
    assert "admin" in user["scopes"]


def test_authenticate_admin_invalid_password(monkeypatch):
    plain = "CorrectAdminPassword99!"
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    settings, sec = bootstrap_auth_env(monkeypatch, password_hash=hashed, admin_users="chief_admin")

    user = sec.authenticate_admin("chief_admin", "WrongGuess123")
    assert user is None


def test_authenticate_admin_rejects_session_secret(monkeypatch):
    plain = "RealPassword123"
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    settings, sec = bootstrap_auth_env(monkeypatch, password_hash=hashed, admin_users="chief_admin")

    # Attempting to use SESSION_SECRET as password MUST be rejected
    session_secret_str = settings.session_secret.get_secret_value()
    user = sec.authenticate_admin("chief_admin", session_secret_str)
    assert user is None


def test_authenticate_admin_missing_hash_fails_safely(monkeypatch):
    # When ADMIN_PASSWORD_HASH is not configured in environment
    settings, sec = bootstrap_auth_env(monkeypatch, password_hash=None, admin_users="chief_admin")
    assert settings.admin_password_hash is None

    user = sec.authenticate_admin("chief_admin", "any_password")
    assert user is None


def test_authenticate_admin_unauthorized_user(monkeypatch):
    plain = "RealPassword123"
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    settings, sec = bootstrap_auth_env(monkeypatch, password_hash=hashed, admin_users="chief_admin")

    user = sec.authenticate_admin("intruder", plain)
    assert user is None


def test_token_creation_and_payload(monkeypatch):
    settings, sec = bootstrap_auth_env(monkeypatch, password_hash=None, admin_users="chief_admin")
    token = sec.create_access_token("chief_admin")
    decoded = jwt.decode(token, settings.session_secret.get_secret_value(), algorithms=[sec.ALGORITHM])
    assert decoded["sub"] == "chief_admin"
    assert "exp" in decoded


@pytest.mark.asyncio
async def test_token_endpoint_flow(monkeypatch):
    plain = "StrongVaultPass456!"
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    settings, sec = bootstrap_auth_env(monkeypatch, password_hash=hashed, admin_users="admin")

    from main import create_app
    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Valid login
        resp = await client.post("/api/token", data={"username": "admin", "password": plain})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        token = data["access_token"]

        # 2. Authenticated /api/me check
        me_resp = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert me_resp.status_code == 200
        assert me_resp.json()["username"] == "admin"

        # 3. Reject SESSION_SECRET as password
        secret_attempt = await client.post(
            "/api/token",
            data={"username": "admin", "password": settings.session_secret.get_secret_value()},
        )
        assert secret_attempt.status_code == 401

        # 4. Reject invalid password
        wrong_attempt = await client.post(
            "/api/token",
            data={"username": "admin", "password": "wrong_password"},
        )
        assert wrong_attempt.status_code == 401
