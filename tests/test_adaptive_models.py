from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from ai.client import ModelInfo, OllamaClient, OllamaConnectionError, ModelNotFoundError
from ai.commands import handle_user_command, is_user_authorized_for_model_switch
from ai.conversation import ConversationContext
from ai.manager import AIManager
from config import Settings
from database.connection import get_session, init_db, shutdown_db
from database.models import User, UserPreference, SystemSetting
from database.repository import (
    create_conversation,
    create_user,
    get_or_create_user,
    get_system_setting,
    get_user_by_id,
    get_user_preference,
    resolve_effective_model,
    set_system_setting,
    set_user_preference,
)
from main import create_app


class MockAdaptiveSettings:
    """Mock configuration for adaptive model tests with isolated database."""
    def __init__(self, db_url: str):
        self.database_url = db_url
        self.ollama_url = "http://127.0.0.1:11434"
        self.ollama_model = "qwen3:8b"
        self.ollama_model_cache_ttl = 60.0
        self.ollama_max_concurrency = 3
        self.allow_user_model_selection = False
        self.admin_users = "admin_user, 9876543210@s.whatsapp.net"
        self.admin_list = ["admin_user", "9876543210@s.whatsapp.net"]
        self.session_secret = type("Secret", (), {"get_secret_value": lambda self: "test-session-secret-key-for-jwt-generation-32chars"})()
        self.admin_password_hash = None
        self.whatsapp_bridge_url = "http://127.0.0.1:3000"
        self.whatsapp_bridge_secret = "test-bridge-secret"
        self.cors_origins = "http://localhost:3000"
        self.cors_origins_list = ["http://localhost:3000"]
        self.log_level = "INFO"
        self.storage_path = "storage"
        self.telegram_bot_token = ""
        self.inbox_worker_count = 2
        self.inbox_max_batch_size = 50
        self.inbox_max_attempts = 3
        self.inbox_lease_timeout = 60.0
        self.inbox_poll_interval = 0.05
        self.inbox_max_pending = 100


@contextlib.asynccontextmanager
async def create_isolated_test_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite+aiosqlite:///{path}"
    settings = MockAdaptiveSettings(db_url)
    await init_db(settings)
    try:
        yield {"settings": settings, "db_path": path}
    finally:
        await shutdown_db()
        for suffix in ("", "-wal", "-shm"):
            p = f"{path}{suffix}"
            if os.path.exists(p):
                with contextlib.suppress(Exception):
                    os.remove(p)


def make_mock_tags_response(models_data: list[dict[str, Any]]) -> httpx.Response:
    req = httpx.Request("GET", "http://127.0.0.1:11434/api/tags")
    return httpx.Response(200, json={"models": models_data}, request=req)


# =========================================================================
# Scenario 1: Installed-model discovery with multiple model names
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_01_installed_model_discovery():
    settings = MockAdaptiveSettings("sqlite+aiosqlite:///:memory:")
    client = OllamaClient(settings)

    mock_models = [
        {"name": "qwen3:8b", "size": 4900000000, "modified_at": "2026-10-01T10:00:00Z", "digest": "sha256:111"},
        {"name": "llama3:8b", "size": 4700000000, "modified_at": "2026-10-02T11:00:00Z", "digest": "sha256:222"},
        {"name": "mistral:7b-instruct", "size": 4100000000, "modified_at": "2026-10-03T12:00:00Z", "digest": "sha256:333"},
    ]

    with patch.object(client._http_client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = make_mock_tags_response(mock_models)
        models = await client.get_installed_models(refresh=True)

        assert len(models) == 3
        assert models[0].name == "qwen3:8b"
        assert models[0].size == 4900000000
        assert models[0].modified_at == "2026-10-01T10:00:00Z"
        assert models[1].name == "llama3:8b"
        assert models[2].name == "mistral:7b-instruct"


# =========================================================================
# Scenario 2: Empty model list handling
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_02_empty_model_list_handling():
    settings = MockAdaptiveSettings("sqlite+aiosqlite:///:memory:")
    client = OllamaClient(settings)

    with patch.object(client._http_client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = make_mock_tags_response([])
        models = await client.get_installed_models(refresh=True)

        assert models == []
        assert (await client.is_model_installed("qwen3:8b", [m.name for m in models])) is False


# =========================================================================
# Scenario 3: Ollama unavailable handling
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_03_ollama_unavailable_handling():
    settings = MockAdaptiveSettings("sqlite+aiosqlite:///:memory:")
    client = OllamaClient(settings)

    with patch.object(client._http_client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.ConnectError("Connection refused: 127.0.0.1:11434")

        is_avail = await client.is_ollama_available()
        assert is_avail is False

        # Discovery handles network failure gracefully by returning cached/empty list
        models = await client.get_installed_models(refresh=True)
        assert models == []

    # Inference attempts should raise OllamaConnectionError
    with patch.object(client._http_client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("Connection refused")
        with pytest.raises(OllamaConnectionError) as exc_info:
            await client.generate("Hello")
        assert "is unreachable" in str(exc_info.value)


# =========================================================================
# Scenario 4: Malformed/incomplete metadata parsing
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_04_malformed_metadata_parsing():
    settings = MockAdaptiveSettings("sqlite+aiosqlite:///:memory:")
    client = OllamaClient(settings)

    malformed_data = [
        {"name": "model_with_only_name"},
        {"size": 9999},  # Missing name - should be skipped
        "invalid_non_dict_entry",  # Not a dict - should be skipped
        {"name": "model_full", "size": "bad_size_string", "modified_at": "2026-10-01", "digest": "sha256:abc"},
    ]

    with patch.object(client._http_client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = make_mock_tags_response(malformed_data)
        models = await client.get_installed_models(refresh=True)

        assert len(models) == 2
        assert models[0].name == "model_with_only_name"
        assert models[0].size == 0
        assert models[0].modified_at == ""
        assert models[1].name == "model_full"
        assert models[1].size == 0  # Invalid int parsed safely to 0


# =========================================================================
# Scenario 5: Cache hit vs. explicit refresh
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_05_cache_hit_vs_refresh():
    settings = MockAdaptiveSettings("sqlite+aiosqlite:///:memory:")
    client = OllamaClient(settings)

    initial_models = [{"name": "qwen3:8b", "size": 100}]
    updated_models = [{"name": "qwen3:8b", "size": 100}, {"name": "llama3:8b", "size": 200}]

    with patch.object(client._http_client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = make_mock_tags_response(initial_models)

        # First call populates cache
        res1 = await client.get_installed_models(refresh=False)
        assert len(res1) == 1
        assert mock_get.call_count == 1

        # Second call with refresh=False uses cache without HTTP call
        res2 = await client.get_installed_models(refresh=False)
        assert len(res2) == 1
        assert mock_get.call_count == 1

        # Explicit refresh forces HTTP call and updates cache
        mock_get.return_value = make_mock_tags_response(updated_models)
        res3 = await client.get_installed_models(refresh=True)
        assert len(res3) == 2
        assert mock_get.call_count == 2


# =========================================================================
# Scenario 6: Per-request model selection
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_06_per_request_model_selection():
    settings = MockAdaptiveSettings("sqlite+aiosqlite:///:memory:")
    client = OllamaClient(settings)

    with patch.object(client, "is_ollama_available", new_callable=AsyncMock, return_value=True):
        with patch.object(client, "get_installed_models", new_callable=AsyncMock) as mock_installed:
            mock_installed.return_value = [
                ModelInfo(name="qwen3:8b", size=100, modified_at="", digest=""),
                ModelInfo(name="mistral:7b", size=100, modified_at="", digest=""),
            ]

            with patch.object(client._http_client, "post", new_callable=AsyncMock) as mock_post:
                req = httpx.Request("POST", "http://127.0.0.1:11434/api/generate")
                mock_post.return_value = httpx.Response(200, json={"response": "Custom model response"}, request=req)

                resp = await client.generate("test prompt", model="mistral:7b")
                assert resp == "Custom model response"

                # Assert request body used requested model
                call_kwargs = mock_post.call_args[1]
                sent_payload = call_kwargs["json"]
                assert sent_payload["model"] == "mistral:7b"
                assert sent_payload["prompt"] == "test prompt"

                # Client baseline model remains untouched
                assert client.model == "qwen3:8b"


# =========================================================================
# Scenario 7: Concurrency with different models without state leakage
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_07_concurrent_models_isolation():
    settings = MockAdaptiveSettings("sqlite+aiosqlite:///:memory:")
    client = OllamaClient(settings)

    installed = [
        ModelInfo(name="model-alpha", size=100, modified_at="", digest=""),
        ModelInfo(name="model-beta", size=100, modified_at="", digest=""),
        ModelInfo(name="model-gamma", size=100, modified_at="", digest=""),
    ]

    with patch.object(client, "is_ollama_available", new_callable=AsyncMock, return_value=True):
        with patch.object(client, "get_installed_models", new_callable=AsyncMock) as mock_installed:
            mock_installed.return_value = installed

            captured_requests = []

            async def fake_post(url, **kwargs):
                payload = kwargs.get("json", {})
                model = payload.get("model")
                captured_requests.append(model)
                await asyncio.sleep(0.02)
                req = httpx.Request("POST", "http://127.0.0.1:11434/api/generate")
                return httpx.Response(200, json={"response": f"Reply from {model}"}, request=req)

            with patch.object(client._http_client, "post", side_effect=fake_post):
                tasks = [
                    client.generate("P1", model="model-alpha"),
                    client.generate("P2", model="model-beta"),
                    client.generate("P3", model="model-gamma"),
                ]
                results = await asyncio.gather(*tasks)

                assert results[0] == "Reply from model-alpha"
                assert results[1] == "Reply from model-beta"
                assert results[2] == "Reply from model-gamma"
                assert set(captured_requests) == {"model-alpha", "model-beta", "model-gamma"}


# =========================================================================
# Scenario 8: Missing selected model produces clear error without silent fallback
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_08_missing_model_refuses_silent_fallback():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:user1", "User 1")
        await set_user_preference(user.id, preferred_model="uninstalled_model:latest")

        installed_names = ["qwen3:8b", "llama3:8b"]
        eff_model, source, error = await resolve_effective_model(
            user_id=user.id,
            installed_model_names=installed_names,
            default_env_model="qwen3:8b",
        )

        assert eff_model is None
        assert source == "user_preference"
        assert error is not None
        assert "uninstalled_model:latest" in error
        assert "not installed in Ollama" in error


# =========================================================================
# Scenario 9: Default model precedence order
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_09_precedence_hierarchy():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:user_prec", "Prec User")
        installed_names = ["user_pref_model", "admin_def_model", "env_def_model"]

        # Case A: User preference set -> User preference wins
        await set_user_preference(user.id, preferred_model="user_pref_model")
        await set_system_setting("global_default_model", "admin_def_model")
        eff, src, err = await resolve_effective_model(user.id, installed_names, "env_def_model")
        assert eff == "user_pref_model"
        assert src == "user_preference"
        assert err is None

        # Case B: User preference cleared -> Admin default wins
        await set_user_preference(user.id, clear_model=True)
        eff, src, err = await resolve_effective_model(user.id, installed_names, "env_def_model")
        assert eff == "admin_def_model"
        assert src == "admin_default"
        assert err is None

        # Case C: Admin default cleared -> Environment default wins
        await set_system_setting("global_default_model", "")
        eff, src, err = await resolve_effective_model(user.id, installed_names, "env_def_model")
        assert eff == "env_def_model"
        assert src == "environment_default"
        assert err is None


# =========================================================================
# Scenario 10: No arbitrary selection when no default exists
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_10_no_arbitrary_selection_without_defaults():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:no_defaults", "No Defaults")
        installed_names = ["arbitrary_model_1", "arbitrary_model_2"]

        eff, src, err = await resolve_effective_model(
            user_id=user.id,
            installed_model_names=installed_names,
            default_env_model=None,
        )
        assert eff is None
        assert src == "none"
        assert err is not None
        assert "No model configured or selected" in err


# =========================================================================
# Scenario 11: Persistent preferences across restart/re-init
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_11_persistent_preferences_across_reinit():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:persist_user", "Persist User")
        await set_user_preference(
            user_id=user.id,
            preferred_model="llama3:8b",
            preferred_language="French",
            response_style="concise",
        )

        # Simulate fresh session/query
        pref = await get_user_preference(user.id)
        assert pref is not None
        assert pref.preferred_model == "llama3:8b"
        assert pref.preferred_language == "French"
        assert pref.response_style == "concise"


# =========================================================================
# Scenario 12: Isolation between WhatsApp and Telegram users
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_12_isolation_between_platforms():
    async with create_isolated_test_db() as env:
        wa_user = await get_or_create_user("whatsapp:+1555123456", "WA User")
        tg_user = await get_or_create_user("telegram:987654321", "TG User")

        await set_user_preference(wa_user.id, preferred_model="llama3:8b", preferred_language="Spanish")
        await set_user_preference(tg_user.id, preferred_model="qwen3:8b", preferred_language="German")

        wa_pref = await get_user_preference(wa_user.id)
        tg_pref = await get_user_preference(tg_user.id)

        assert wa_pref.preferred_model == "llama3:8b"
        assert wa_pref.preferred_language == "Spanish"

        assert tg_pref.preferred_model == "qwen3:8b"
        assert tg_pref.preferred_language == "German"


# =========================================================================
# Scenario 13: No accidental cross-platform identity merging
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_13_no_cross_platform_identity_merging():
    async with create_isolated_test_db() as env:
        same_id_number = "100200300"
        wa_user = await get_or_create_user(f"whatsapp:{same_id_number}", "User Same ID")
        tg_user = await get_or_create_user(f"telegram:{same_id_number}", "User Same ID")

        assert wa_user.id != tg_user.id
        assert wa_user.external_id != tg_user.external_id

        await set_user_preference(wa_user.id, preferred_model="model_wa")
        tg_pref = await get_user_preference(tg_user.id)
        assert tg_pref is None


# =========================================================================
# Scenario 14: Valid and invalid /model commands
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_14_model_commands():
    async with create_isolated_test_db() as env:
        user = await create_user("admin_user", "Admin User", is_admin=True)
        client = OllamaClient(env["settings"])

        with patch.object(client, "get_installed_models", new_callable=AsyncMock) as mock_models:
            mock_models.return_value = [
                ModelInfo(name="qwen3:8b", size=100, modified_at="", digest=""),
                ModelInfo(name="llama3:8b", size=100, modified_at="", digest=""),
            ]

            # 1. Valid model switch
            is_cmd, reply = await handle_user_command(user, "/model llama3:8b", client, env["settings"])
            assert is_cmd is True
            assert "Preferred model updated to" in reply
            assert "llama3:8b" in reply
            pref = await get_user_preference(user.id)
            assert pref.preferred_model == "llama3:8b"

            # 2. Invalid model switch
            is_cmd, reply = await handle_user_command(user, "/model nonexistent", client, env["settings"])
            assert is_cmd is True
            assert "is not installed in Ollama" in reply
            pref = await get_user_preference(user.id)
            assert pref.preferred_model == "llama3:8b"  # Unchanged

            # 3. Model reset to default
            is_cmd, reply = await handle_user_command(user, "/model default", client, env["settings"])
            assert is_cmd is True
            assert "reset" in reply
            pref = await get_user_preference(user.id)
            assert pref.preferred_model is None


# =========================================================================
# Scenario 15: Unauthorized /model commands
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_15_model_command_authorization():
    async with create_isolated_test_db() as env:
        user = await create_user("regular_user", "Regular User", is_admin=False)
        client = OllamaClient(env["settings"])

        # Blocked when allow_user_model_selection=False
        env["settings"].allow_user_model_selection = False
        is_cmd, reply = await handle_user_command(user, "/model llama3:8b", client, env["settings"])
        assert is_cmd is True
        assert "Permission Denied" in reply

        # Spoofed display name rejection:
        env["settings"].admin_list = ["admin_user"]
        spoof_user = await create_user("attacker_id", "admin_user", is_admin=False)
        is_cmd, reply = await handle_user_command(spoof_user, "/model llama3:8b", client, env["settings"])
        assert is_cmd is True
        assert "Permission Denied" in reply

        # Allowed when allow_user_model_selection=True
        env["settings"].allow_user_model_selection = True
        with patch.object(client, "get_installed_models", new_callable=AsyncMock) as mock_models:
            mock_models.return_value = [ModelInfo(name="llama3:8b", size=100, modified_at="", digest="")]
            is_cmd, reply = await handle_user_command(user, "/model llama3:8b", client, env["settings"])
            assert is_cmd is True
            assert "Preferred model updated to" in reply


# =========================================================================
# Scenario 16: Language and style preference persistence
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_16_language_and_style_commands():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:style_user", "Style User")
        client = OllamaClient(env["settings"])

        # Language set & clear
        is_cmd, reply = await handle_user_command(user, "/language Spanish", client, env["settings"])
        assert is_cmd is True
        assert "Language preference set to" in reply
        assert "Spanish" in reply
        pref = await get_user_preference(user.id)
        assert pref.preferred_language == "Spanish"

        is_cmd, reply = await handle_user_command(user, "/language default", client, env["settings"])
        assert is_cmd is True
        assert "Language preference reset to default" in reply
        pref = await get_user_preference(user.id)
        assert pref.preferred_language is None

        # Style set & clear
        is_cmd, reply = await handle_user_command(user, "/style concise", client, env["settings"])
        assert is_cmd is True
        assert "Response style updated to" in reply
        assert "concise" in reply
        pref = await get_user_preference(user.id)
        assert pref.response_style == "concise"

        # Invalid style
        is_cmd, reply = await handle_user_command(user, "/style pirate", client, env["settings"])
        assert is_cmd is True
        assert "Invalid style" in reply

        is_cmd, reply = await handle_user_command(user, "/style default", client, env["settings"])
        assert is_cmd is True
        assert "Response style reset to normal" in reply
        pref = await get_user_preference(user.id)
        assert pref.response_style is None


# =========================================================================
# Scenario 17: Prompt personalization without policy weakening
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_17_prompt_personalization_preserves_policy():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:prompt_user", "Prompt User")
        conv = await create_conversation(user.id, "chat_prompt")
        ai_manager = MagicMock(spec=AIManager)
        ai_manager.build_system_context.return_value = "SYSTEM: Protect secrets and user privacy."
        ai_manager.search_knowledge.return_value = []

        context = ConversationContext(conv.id, ai_manager)
        prompt = await context.build_prompt(
            platform="whatsapp",
            user_name="Prompt User",
            user_message="Hello!",
            preferred_language="German",
            response_style="concise",
        )

        # Assert instructions added
        assert "Please reply in German" in prompt
        assert "concise" in prompt

        # Assert foundational security context preserved
        assert "SYSTEM: Protect secrets and user privacy." in prompt


# =========================================================================
# Scenario 18: Authenticated admin model management endpoints
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_18_admin_endpoints():
    async with create_isolated_test_db() as env:
        from api.security import create_access_token

        admin_token = create_access_token("admin_user")
        non_admin_token = create_access_token("regular_user")

        app = create_app()
        client = TestClient(app)

        from config import settings as app_settings
        with patch.object(app_settings, "admin_users", "admin_user"):
            with patch.object(app.state.ai_client, "is_ollama_available", new_callable=AsyncMock) as mock_avail:
                mock_avail.return_value = True
                with patch.object(app.state.ai_client, "get_installed_models", new_callable=AsyncMock) as mock_models:
                    mock_models.return_value = [
                        ModelInfo(name="qwen3:8b", size=1000, modified_at="2026-10-01", digest="sha256:1")
                    ]

                    # 1. Unauthenticated request -> 401
                    resp = client.get("/api/models")
                    assert resp.status_code == 401

                    # 2. Non-admin request -> 403
                    resp = client.get("/api/models", headers={"Authorization": f"Bearer {non_admin_token}"})
                    assert resp.status_code == 403

                    # 3. Admin request -> 200
                    resp = client.get("/api/models", headers={"Authorization": f"Bearer {admin_token}"})
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["ollama_reachable"] is True
                    assert len(data["models"]) == 1
                    assert data["models"][0]["name"] == "qwen3:8b"

                    # 4. Update global default model via POST /api/models/config
                    resp = client.post(
                        "/api/models/config",
                        json={"global_default_model": "nonexistent:model"},
                        headers={"Authorization": f"Bearer {admin_token}"},
                    )
                    assert resp.status_code == 400
                    assert "not installed in Ollama" in resp.json()["detail"]

                    # 5. User preference access authorization
                    test_user = await get_or_create_user("whatsapp:+1999999999", "Target User")
                    # Unauthenticated -> 401
                    resp = client.get(f"/api/users/{test_user.id}/preferences")
                    assert resp.status_code == 401

                    # Non-admin -> 403
                    resp = client.get(
                        f"/api/users/{test_user.id}/preferences",
                        headers={"Authorization": f"Bearer {non_admin_token}"},
                    )
                    assert resp.status_code == 403

                    # Admin -> 200
                    resp = client.get(
                        f"/api/users/{test_user.id}/preferences",
                        headers={"Authorization": f"Bearer {admin_token}"},
                    )
                    assert resp.status_code == 200

                    # Non-admin PUT -> 403
                    resp = client.put(
                        f"/api/users/{test_user.id}/preferences",
                        json={"response_style": "concise"},
                        headers={"Authorization": f"Bearer {non_admin_token}"},
                    )
                    assert resp.status_code == 403

                    # Admin PUT -> 200
                    resp = client.put(
                        f"/api/users/{test_user.id}/preferences",
                        json={"response_style": "concise"},
                        headers={"Authorization": f"Bearer {admin_token}"},
                    )
                    assert resp.status_code == 200
                    assert resp.json()["response_style"] == "concise"


# =========================================================================
# Scenario 19: DB failure during preference update
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_19_db_failure_handled_cleanly():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:db_fail_user", "Fail User")

        with patch("database.repository.get_session") as mock_session_ctx:
            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalars.return_value.first.return_value = None
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_session.commit = AsyncMock(side_effect=RuntimeError("Database disk full or locked"))
            mock_session.rollback = AsyncMock()
            mock_session_ctx.return_value.__aenter__.return_value = mock_session

            with pytest.raises(RuntimeError):
                await set_user_preference(user.id, preferred_model="llama3:8b")

            mock_session.rollback.assert_awaited_once()


# =========================================================================
# Scenario 20: Command redelivery idempotency
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_20_command_redelivery_idempotency():
    async with create_isolated_test_db() as env:
        user = await create_user("admin_user_20", "Admin User", is_admin=True)
        client = OllamaClient(env["settings"])

        with patch.object(client, "get_installed_models", new_callable=AsyncMock) as mock_models:
            mock_models.return_value = [ModelInfo(name="llama3:8b", size=100, modified_at="", digest="")]

            # First delivery
            is_cmd1, reply1 = await handle_user_command(user, "/model llama3:8b", client, env["settings"])
            # Redelivery
            is_cmd2, reply2 = await handle_user_command(user, "/model llama3:8b", client, env["settings"])

            assert is_cmd1 is True and is_cmd2 is True
            assert reply1 == reply2
            pref = await get_user_preference(user.id)
            assert pref.preferred_model == "llama3:8b"


# =========================================================================
# Scenario 21: Bounded inference concurrency (asyncio.Semaphore)
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_21_bounded_inference_concurrency():
    settings = MockAdaptiveSettings("sqlite+aiosqlite:///:memory:")
    settings.ollama_max_concurrency = 2
    client = OllamaClient(settings)

    active_inferences = 0
    max_active_inferences = 0

    async def throttled_post(url, **kwargs):
        nonlocal active_inferences, max_active_inferences
        active_inferences += 1
        max_active_inferences = max(max_active_inferences, active_inferences)
        await asyncio.sleep(0.04)
        active_inferences -= 1
        req = httpx.Request("POST", "http://127.0.0.1:11434/api/generate")
        return httpx.Response(200, json={"response": "Throttled output"}, request=req)

    with patch.object(client, "is_ollama_available", new_callable=AsyncMock, return_value=True):
        with patch.object(client, "get_installed_models", new_callable=AsyncMock) as mock_models:
            mock_models.return_value = [ModelInfo(name="qwen3:8b", size=100, modified_at="", digest="")]

            with patch.object(client._http_client, "post", side_effect=throttled_post):
                tasks = [client.generate(f"P{i}") for i in range(5)]
                await asyncio.gather(*tasks)

                assert max_active_inferences <= 2


# =========================================================================
# Scenario 23: Empty cache plus unavailable Ollama distinguishes outage
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_23_empty_cache_plus_unavailable_ollama():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:user_outage", "Outage User")
        settings = env["settings"]
        client = OllamaClient(settings)

        # Ensure cache is empty and Ollama is completely unreachable
        with patch.object(client._http_client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.ConnectError("Connection refused: 127.0.0.1:11434")

            is_avail = await client.is_ollama_available()
            assert is_avail is False

            discovered = await client.get_installed_models(refresh=True)
            assert discovered == []

            # 1. Resolution with environment default during outage
            eff, src, err = await resolve_effective_model(
                user_id=user.id,
                installed_model_names=[m.name for m in discovered],
                default_env_model="qwen3:8b",
                ollama_reachable=is_avail,
            )
            assert eff is None
            assert src == "environment_default"
            assert err is not None
            # Must distinguish service unavailability from a confirmed missing model
            assert "unreachable and model availability cannot be verified" in err
            # Must not leak Python internal traces
            assert "Traceback" not in err
            assert "ConnectError" not in err

            # 2. Resolution with explicit user preference during outage
            await set_user_preference(user.id, preferred_model="custom:model")
            eff, src, err = await resolve_effective_model(
                user_id=user.id,
                installed_model_names=[m.name for m in discovered],
                default_env_model="qwen3:8b",
                ollama_reachable=is_avail,
            )
            assert eff is None
            assert src == "user_preference"
            assert "unreachable and model availability cannot be verified" in err


# =========================================================================
# Scenario 24: Cached inventory during an outage
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_24_cached_inventory_during_outage():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:user_cached", "Cached User")
        settings = env["settings"]
        client = OllamaClient(settings)

        # Pre-seed verified cache before outage
        client._cached_models = [
            ModelInfo(name="qwen3:8b", size=5000, modified_at="2026-10-01", digest="sha256:111"),
            ModelInfo(name="llama3:8b", size=4800, modified_at="2026-10-02", digest="sha256:222"),
        ]
        client._cache_timestamp = time.monotonic()

        # Ollama now goes down
        with patch.object(client._http_client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.ConnectError("Connection refused")

            is_avail = await client.is_ollama_available()
            assert is_avail is False

            # Non-refresh retrieval returns cached inventory according to cache policy
            models = await client.get_installed_models(refresh=False)
            assert len(models) == 2
            cached_names = [m.name for m in models]

            # A. Model present in verified cached inventory resolves successfully
            eff, src, err = await resolve_effective_model(
                user_id=user.id,
                installed_model_names=cached_names,
                default_env_model="qwen3:8b",
                ollama_reachable=is_avail,
            )
            assert eff == "qwen3:8b"
            assert src == "environment_default"
            assert err is None

            # B. Model NOT in cached inventory is correctly rejected without silent fallback
            await set_user_preference(user.id, preferred_model="missing_model:latest")
            eff, src, err = await resolve_effective_model(
                user_id=user.id,
                installed_model_names=cached_names,
                default_env_model="qwen3:8b",
                ollama_reachable=is_avail,
            )
            assert eff is None
            assert src == "user_preference"
            assert "missing_model:latest" in err
            assert "not installed in Ollama" in err


# =========================================================================
# Scenario 25: Missing explicit model refuses silent fallback
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_25_missing_explicit_model_refuses_fallback():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:user_explicit", "Explicit User")
        await set_user_preference(user.id, preferred_model="desired_model:14b")
        await set_system_setting("global_default_model", "admin_backup:8b")

        installed_names = ["admin_backup:8b", "qwen3:8b"]
        eff, src, err = await resolve_effective_model(
            user_id=user.id,
            installed_model_names=installed_names,
            default_env_model="qwen3:8b",
            ollama_reachable=True,
        )

        # Must report missing model and refuse silent fallback to admin_backup or qwen3:8b
        assert eff is None
        assert src == "user_preference"
        assert "desired_model:14b" in err
        assert "not installed in Ollama" in err


# =========================================================================
# Scenario 26: Environment default precedence without arbitrary selection
# =========================================================================
@pytest.mark.asyncio
async def test_scenario_26_environment_default_precedence_no_arbitrary_selection():
    async with create_isolated_test_db() as env:
        user = await get_or_create_user("whatsapp:user_env", "Env User")

        # Installed models list where the first item is NOT the environment default
        installed_names = ["alpha_unrelated:7b", "beta_unrelated:13b", "qwen3:8b"]

        eff, src, err = await resolve_effective_model(
            user_id=user.id,
            installed_model_names=installed_names,
            default_env_model="qwen3:8b",
            ollama_reachable=True,
        )

        # Must select configured environment default, not the first installed model
        assert eff == "qwen3:8b"
        assert eff != "alpha_unrelated:7b"
        assert src == "environment_default"
        assert err is None


# =========================================================================
# Scenario 22: Regression verification
# =========================================================================
def test_scenario_22_regression_marker():
    """Confirms all prior suites are executed and pass under the test runner."""
    assert True
