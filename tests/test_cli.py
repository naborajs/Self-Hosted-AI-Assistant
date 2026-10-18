from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from config import settings
from edith import __version__
from edith.cli import main
from edith.client import (
    EdithApiAuthError,
    EdithApiConnectionError,
    EdithApiError,
    EdithApiRequestError,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_client():
    client = MagicMock()
    # Default mocks for happy paths
    client.base_url = "http://127.0.0.1:8000"
    client.get_overview.return_value = {
        "status": "healthy",
        "system": {
            "version": "2.0.0",
            "host": "127.0.0.1",
            "port": 8000,
            "loopback_only": True,
            "remote_api_allowed": False,
        },
        "dependencies": {
            "database": {"connected": True, "journal_mode": "wal"},
            "ollama": {
                "reachable": True,
                "installed_model_count": 2,
                "effective_default_model": "mistral:latest",
            },
            "whatsapp": {"configured": True, "reachable": True, "connected": True},
            "telegram": {"configured": True},
        },
        "queues": {
            "inbound": {"received": 1, "completed": 10, "failed": 0},
            "outbound": {"pending": 0, "sent": 12, "failed": 1, "ambiguous": 0},
        },
        "tools_count": 5,
        "pending_actions_count": 0,
        "warnings": [],
    }
    client.list_models.return_value = {
        "models": [
            {"name": "mistral:latest", "size": "4.1 GB", "modified": "2026-10-15"},
            {"name": "llama3:8b", "size": "4.7 GB", "modified": "2026-10-14"},
        ],
        "count": 2,
    }
    client.get_model_config.return_value = {
        "configured_default_model": "mistral:latest",
        "global_default_model": "mistral:latest",
        "fallback_default_model": "mistral:latest",
        "effective_default_model": "mistral:latest",
        "dynamic_discovery_enabled": True,
    }
    client.set_default_model.return_value = {
        "status": "updated",
        "default_model": "llama3:8b",
    }
    client.list_users.return_value = {
        "users": [
            {
                "id": 1,
                "external_id_masked": "whatsapp:***7890",
                "display_name": "Test User",
                "is_admin": True,
                "preferred_model": "mistral:latest",
                "effective_model": "mistral:latest",
                "conversation_count": 4,
            }
        ],
        "total": 1,
        "limit": 50,
        "offset": 0,
    }
    client.get_user.return_value = {
        "id": 1,
        "external_id_masked": "whatsapp:***7890",
        "display_name": "Test User",
        "is_admin": True,
        "preferred_model": "mistral:latest",
        "effective_model": "mistral:latest",
        "conversation_count": 4,
        "pending_actions_count": 0,
    }
    client.list_memory.return_value = {
        "user_id": 1,
        "query": None,
        "count": 1,
        "memory": [
            {
                "key": "user_name",
                "value": "Alice",
                "tags": "profile",
                "created_at": "2026-10-15T10:00:00",
                "updated_at": "2026-10-15T10:00:00",
            }
        ],
    }
    client.save_memory.return_value = {
        "status": "saved",
        "user_id": 1,
        "key": "user_name",
        "value": "Alice",
    }
    client.delete_memory.return_value = {
        "status": "deleted",
        "user_id": 1,
        "key": "user_name",
    }
    client.list_tools.return_value = {
        "policy": {
            "enable_tool_execution": True,
            "allow_external_network_tools": False,
            "tool_max_rounds": 5,
            "tool_max_calls_per_request": 3,
            "tool_timeout_seconds": 15,
            "tool_max_output_chars": 4000,
        },
        "tools": [
            {
                "name": "calculate",
                "description": "Evaluate math expression",
                "permission_level": "read_only",
                "requires_human_confirmation": False,
                "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}},
            },
            {
                "name": "write_file",
                "description": "Write to workspace file",
                "permission_level": "workspace_write",
                "requires_human_confirmation": True,
                "parameters": {"type": "object", "properties": {"filename": {"type": "string"}}},
            },
        ],
    }
    client.get_deliveries.return_value = {
        "inbound": [
            {
                "id": 1,
                "platform": "whatsapp",
                "chat_id": "chat-101",
                "sender": "***7890",
                "content_preview": "Hello EDITH",
                "status": "COMPLETED",
                "attempt_count": 1,
                "last_error": None,
                "received_at": "2026-10-15T12:00:00",
            }
        ],
        "outbound": [
            {
                "id": 10,
                "platform": "whatsapp",
                "chat_id": "chat-101",
                "recipient_id": "***7890",
                "content_preview": "Hello! How can I assist?",
                "status": "FAILED",
                "attempt_count": 3,
                "max_attempts": 3,
                "correlation_id": "corr-101",
                "last_error": "Timeout",
                "created_at": "2026-10-15T12:01:00",
            }
        ],
    }
    client.retry_outbound.return_value = {
        "status": "requeued",
        "message_id": 10,
        "new_status": "PENDING",
    }
    client.get_integrations.return_value = {
        "whatsapp": {
            "configured": True,
            "bridge_url": "http://127.0.0.1:3000",
            "reachable": True,
            "connected": True,
            "authenticated": True,
            "phone": "***7890",
        },
        "telegram": {
            "configured": True,
            "token_masked": "123456:***ew11",
            "polling_active": True,
        },
    }
    return client


# ============================================================================
# 1. System & Global CLI Tests
# ============================================================================

def test_cli_help(runner):
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "EDITH" in result.output
    assert "status" in result.output
    assert "doctor" in result.output
    assert "models" in result.output
    assert "users" in result.output
    assert "memory" in result.output
    assert "tools" in result.output
    assert "queue" in result.output
    assert "integrations" in result.output


def test_cli_version(runner):
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output

    result2 = runner.invoke(main, ["version"])
    assert result2.exit_code == 0
    assert __version__ in result2.output

    result_json = runner.invoke(main, ["version", "--json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)
    assert data["name"] == "EDITH"
    assert data["version"] == __version__
    assert data["operating_model"] == "local-first-inference"


def test_cli_auth_failure_exit_code_2(runner):
    with patch("edith.cli.get_client") as mock_gc:
        client = MagicMock()
        client.get_overview.side_effect = EdithApiAuthError("Invalid credentials")
        mock_gc.return_value = client

        result = runner.invoke(main, ["status"])
        assert result.exit_code == 2
        assert "Authentication Error" in result.output

        result_json = runner.invoke(main, ["status", "--json"])
        assert result_json.exit_code == 2
        data = json.loads(result_json.output)
        assert data["exit_code"] == 2
        assert data["code"] == 401


def test_cli_connection_failure_exit_code_1(runner):
    with patch("edith.cli.get_client") as mock_gc:
        client = MagicMock()
        client.base_url = "http://127.0.0.1:8000"
        client.get_overview.side_effect = EdithApiConnectionError("Connection refused")
        mock_gc.return_value = client

        result = runner.invoke(main, ["status"])
        assert result.exit_code == 1
        assert "OFFLINE" in result.output

        result_json = runner.invoke(main, ["status", "--json"])
        assert result_json.exit_code == 1
        data = json.loads(result_json.output)
        assert data["backend_online"] is False


def test_cli_status_success(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "HEALTHY" in result.output

        result_json = runner.invoke(main, ["status", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert data["status"] == "healthy"


def test_cli_doctor_command(runner):
    # Test doctor with mocked reachable dependencies
    with patch.object(settings, "admin_password_hash", SecretStr("mock_hash")), \
         patch("httpx.Client") as mock_http:
        mock_resp_ollama = MagicMock()
        mock_resp_ollama.status_code = 200
        mock_resp_ollama.json.return_value = {"models": [{"name": settings.ollama_model}]}

        mock_resp_bridge = MagicMock()
        mock_resp_bridge.status_code = 200

        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.get.side_effect = lambda url: mock_resp_ollama if "tags" in str(url) else mock_resp_bridge
        mock_http.return_value = mock_instance

        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "All diagnostic checks passed" in result.output

        result_json = runner.invoke(main, ["doctor", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert data["all_passed"] is True
        assert len(data["doctor_checks"]) >= 8


def test_cli_doctor_command_failure_exit_code_1(runner):
    # Simulate Ollama offline
    with patch("httpx.Client") as mock_http:
        mock_instance = MagicMock()
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.get.side_effect = Exception("Connection refused")
        mock_http.return_value = mock_instance

        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 1
        assert "FAIL" in result.output or "FAILED" in result.output


def test_cli_config_check(runner):
    result = runner.invoke(main, ["config", "check"])
    assert result.exit_code == 0
    assert "Active EDITH Configuration" in result.output
    # Secret values must be boolean flags, not raw secrets
    assert "has_session_secret" in result.output
    assert "has_admin_password_hash" in result.output

    result_json = runner.invoke(main, ["config", "check", "--json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)
    assert "app_host" in data
    assert "is_loopback_only" in data
    assert "allow_remote_api" in data
    assert "has_session_secret" in data


def test_cli_logs_stdout(runner):
    result = runner.invoke(main, ["logs"])
    assert result.exit_code == 0

    result_json = runner.invoke(main, ["logs", "--json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)
    assert "log_file" in data or "file" in data


# ============================================================================
# 2. Models Management Commands
# ============================================================================

def test_cli_models_list(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["models", "list"])
        assert result.exit_code == 0
        assert "mistral:latest" in result.output
        assert "llama3:8b" in result.output

        result_json = runner.invoke(main, ["models", "list", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert data["count"] == 2


def test_cli_models_status(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["models", "status"])
        assert result.exit_code == 0
        assert "Ollama Model Configuration" in result.output

        result_json = runner.invoke(main, ["models", "status", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert data["effective_default_model"] == "mistral:latest"


def test_cli_models_default(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["models", "default"])
        assert result.exit_code == 0
        assert "mistral:latest" in result.output

        result_json = runner.invoke(main, ["models", "default", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert data["effective_default_model"] == "mistral:latest"


def test_cli_models_set_default(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["models", "set-default", "llama3:8b"])
        assert result.exit_code == 0
        assert "Global default model updated to" in result.output
        assert "llama3:8b" in result.output
        mock_client.set_default_model.assert_called_with(model_name="llama3:8b")

        result_json = runner.invoke(main, ["models", "set-default", "llama3:8b", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert data["default_model"] == "llama3:8b"


def test_cli_models_refresh(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["models", "refresh"])
        assert result.exit_code == 0
        assert "Model cache refreshed" in result.output
        mock_client.list_models.assert_called_with(refresh=True)


# ============================================================================
# 3. Users and Memory Commands
# ============================================================================

def test_cli_users_list(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["users", "list"])
        assert result.exit_code == 0
        assert "***7890" in result.output
        assert "Test User" in result.output

        result_json = runner.invoke(main, ["users", "list", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert len(data["users"]) == 1
        assert data["users"][0]["external_id_masked"] == "whatsapp:***7890"


def test_cli_users_show(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["users", "show", "1"])
        assert result.exit_code == 0
        assert "Test User" in result.output

        result_json = runner.invoke(main, ["users", "show", "1", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert data["id"] == 1


def test_cli_memory_list_and_search(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["memory", "list", "--user", "1"])
        assert result.exit_code == 0
        assert "user_name" in result.output
        assert "Alice" in result.output

        result_search = runner.invoke(main, ["memory", "search", "--user", "1", "Alice"])
        assert result_search.exit_code == 0
        assert "Alice" in result_search.output

        result_json = runner.invoke(main, ["memory", "search", "--user", "1", "Alice", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert data["count"] == 1


def test_cli_memory_save(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["memory", "save", "--user", "1", "user_name", "Alice"])
        assert result.exit_code == 0
        assert "Fact saved for User #1" in result.output
        mock_client.save_memory.assert_called_with(user_id=1, key="user_name", value="Alice", tags=None)


def test_cli_memory_delete_confirmation(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        # Aborted confirmation
        result_abort = runner.invoke(main, ["memory", "delete", "--user", "1", "user_name"], input="n\n")
        assert result_abort.exit_code != 0
        mock_client.delete_memory.assert_not_called()

        # Accepted confirmation
        result_confirm = runner.invoke(main, ["memory", "delete", "--user", "1", "user_name"], input="y\n")
        assert result_confirm.exit_code == 0
        assert "Fact 'user_name' status" in result_confirm.output
        mock_client.delete_memory.assert_called_with(user_id=1, key="user_name")

        # Skip confirmation with --yes flag
        result_yes = runner.invoke(main, ["memory", "delete", "--user", "1", "user_name", "--yes"])
        assert result_yes.exit_code == 0


# ============================================================================
# 4. Tools and Security Policy Commands
# ============================================================================

def test_cli_tools_list(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["tools", "list"])
        assert result.exit_code == 0
        assert "calculate" in result.output
        assert "write_file" in result.output

        result_json = runner.invoke(main, ["tools", "list", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert len(data["tools"]) == 2


def test_cli_tools_status_and_policy(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["tools", "status"])
        assert result.exit_code == 0
        assert "tool_execution_enabled" in result.output

        result_policy = runner.invoke(main, ["tools", "policy"])
        assert result_policy.exit_code == 0
        assert "tool_max_rounds" in result_policy.output

        result_validate = runner.invoke(main, ["tools", "validate"])
        assert result_validate.exit_code == 0
        assert "Tool registry valid" in result_validate.output


# ============================================================================
# 5. Queue Diagnostics and Delivery Commands
# ============================================================================

def test_cli_queue_status(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["queue", "status"])
        assert result.exit_code == 0
        assert "Inbound Inbox" in result.output
        assert "Outbound Delivery" in result.output

        result_json = runner.invoke(main, ["queue", "status", "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert "inbound" in data
        assert "outbound" in data


def test_cli_queue_inbound_and_outbound(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result_in = runner.invoke(main, ["queue", "inbound"])
        assert result_in.exit_code == 0
        assert "chat-101" in result_in.output
        assert "COMPLETED" in result_in.output

        result_out = runner.invoke(main, ["queue", "outbound"])
        assert result_out.exit_code == 0
        assert "FAILED" in result_out.output

        result_failed = runner.invoke(main, ["queue", "failed"])
        assert result_failed.exit_code == 0
        assert "corr-101" in result_failed.output


def test_cli_queue_retry_confirmation(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        # Aborted retry
        result_abort = runner.invoke(main, ["queue", "retry", "10"], input="n\n")
        assert result_abort.exit_code != 0
        mock_client.retry_outbound.assert_not_called()

        # Confirmed retry
        result_confirm = runner.invoke(main, ["queue", "retry", "10"], input="y\n")
        assert result_confirm.exit_code == 0
        assert "requeued to PENDING" in result_confirm.output
        mock_client.retry_outbound.assert_called_with(message_id=10)

        # Skip prompt with --yes
        result_yes = runner.invoke(main, ["queue", "retry", "10", "--yes"])
        assert result_yes.exit_code == 0


# ============================================================================
# 6. Messaging Integrations Commands
# ============================================================================

def test_cli_integrations_commands(runner, mock_client):
    with patch("edith.cli.get_client", return_value=mock_client):
        result = runner.invoke(main, ["integrations", "status"])
        assert result.exit_code == 0
        assert "WhatsApp (Baileys Bridge)" in result.output
        assert "Telegram (Bot API)" in result.output

        result_wa = runner.invoke(main, ["integrations", "whatsapp"])
        assert result_wa.exit_code == 0
        assert "http://127.0.0.1:3000" in result_wa.output

        result_tg = runner.invoke(main, ["integrations", "telegram"])
        assert result_tg.exit_code == 0
        assert "123456:***ew11" in result_tg.output
