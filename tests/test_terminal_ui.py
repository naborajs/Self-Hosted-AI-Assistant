from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from edith.terminal_ui import EdithTUIApp


def make_mock_client(online: bool = True):
    client = MagicMock()
    client.base_url = "http://127.0.0.1:8000"
    if not online:
        client.get_overview.side_effect = Exception("Connection refused")
        return client

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
        "warnings": ["Low disk space warning"],
    }
    client.list_models.return_value = {
        "models": [
            {"name": "mistral:latest", "size": "4.1 GB", "modified": "2026-10-15"},
        ],
        "count": 1,
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
            }
        ],
    }
    client.get_integrations.return_value = {
        "whatsapp": {"configured": True, "bridge_url": "http://127.0.0.1:3000", "connected": True, "phone": "***7890"},
        "telegram": {"configured": True, "token_masked": "123***ew11", "polling_active": True},
    }
    client.get_deliveries.return_value = {
        "inbound": [],
        "outbound": [
            {
                "id": 10,
                "platform": "whatsapp",
                "recipient_id": "***7890",
                "status": "FAILED",
                "attempts": 3,
                "correlation_id": "corr-101",
                "last_error": "Timeout",
            }
        ],
    }
    client.list_users.return_value = {
        "users": [
            {
                "id": 1,
                "external_id_masked": "whatsapp:***7890",
                "display_name": "Test User",
                "is_admin": True,
                "effective_model": "mistral:latest",
                "conversation_count": 2,
            }
        ],
        "total": 1,
    }
    return client


@pytest.mark.asyncio
async def test_tui_app_lifecycle_online():
    client = make_mock_client(online=True)
    app = EdithTUIApp(client=client)

    async with app.run_test() as pilot:
        # Give worker a moment to execute data loading
        await pilot.pause()

        assert app.backend_online is True
        overview_widget = app.query_one("#overview-content")
        assert "HEALTHY" in overview_widget.content

        # Test refresh keybinding
        await pilot.press("r")
        await pilot.pause()
        assert app.backend_online is True

        # Test theme toggle and exit keybindings
        await pilot.press("d")
        await pilot.press("q")


@pytest.mark.asyncio
async def test_tui_app_lifecycle_offline():
    client = make_mock_client(online=False)
    app = EdithTUIApp(client=client)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.backend_online is False
        overview_widget = app.query_one("#overview-content")
        assert "OFFLINE" in overview_widget.content

        # Safe exit
        await pilot.press("q")
