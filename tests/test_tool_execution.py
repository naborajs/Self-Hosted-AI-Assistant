from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import re
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ai.client import OllamaClient, ModelNotFoundError, OllamaConnectionError
from ai.commands import handle_user_command
from ai.orchestrator import ToolOrchestrator
from ai.tools import PermissionCategory, ToolDefinition, ToolExecutionResult, ToolRegistry
from database.connection import init_db, shutdown_db
from database.models import PendingAction, User
from database.repository import (
    cancel_pending_action,
    consume_pending_action,
    create_conversation,
    create_pending_action,
    get_active_pending_action,
    get_or_create_user,
)
from tools.calculator import calculate_expression
from tools.file_manager import (
    SafePathError,
    list_files,
    read_file_content,
    resolve_safe_path,
    write_file_content,
)
from tools.search import search_web
from tools.weather import get_weather


class MockToolSettings:
    """Mock application settings with an isolated SQLite database and workspace directory."""

    def __init__(self, db_url: str, workspace_root: str):
        self.database_url = db_url
        self.tool_workspace_root = workspace_root
        self.enable_tool_execution = True
        self.tool_max_rounds = 3
        self.tool_max_calls_per_request = 5
        self.tool_timeout_seconds = 5.0
        self.tool_max_output_chars = 2000
        self.allow_external_network_tools = False
        self.tool_default_permission_policy = "prompt"
        self.ollama_url = "http://127.0.0.1:11434"
        self.ollama_model = "qwen3:8b"
        self.ollama_model_cache_ttl = 60.0
        self.ollama_max_concurrency = 3
        self.allow_user_model_selection = True
        self.admin_users = "admin_user"
        self.admin_list = ["admin_user"]
        self.session_secret = type("Secret", (), {"get_secret_value": lambda self: "test-secret-at-least-32-chars-long!!"})()
        self.admin_password_hash = None
        self.whatsapp_bridge_url = "http://127.0.0.1:3000"
        self.whatsapp_bridge_secret = "test-bridge-secret"
        self.telegram_bot_token = ""
        self.storage_path = Path(tempfile.gettempdir())
        self.encrypted_sessions_path = Path(tempfile.gettempdir())
        self.inbox_worker_count = 2

        self.inbox_max_batch_size = 50
        self.inbox_max_attempts = 3
        self.inbox_lease_timeout = 60.0
        self.inbox_poll_interval = 0.05
        self.inbox_max_pending = 100


@contextlib.asynccontextmanager
async def create_isolated_tool_env():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    test_settings = MockToolSettings(db_url, ws_dir)

    await init_db(test_settings)
    try:
        yield {"settings": test_settings, "db_path": db_path, "ws_dir": ws_dir}
    finally:
        await shutdown_db()
        if os.path.exists(db_path):
            with contextlib.suppress(Exception):
                os.remove(db_path)
        if os.path.exists(ws_dir):
            with contextlib.suppress(Exception):
                shutil.rmtree(ws_dir)


# ============================================================================
# Section 1: Calculator Safety & Bounds Tests (7 scenarios)
# ============================================================================


def test_calculator_basic_arithmetic():
    assert calculate_expression("2 + 2") == "4"
    assert calculate_expression("10 * 5 - 4 / 2") == "48"
    assert calculate_expression("2 ** 10") == "1024"
    assert calculate_expression("-(5 + 3)") == "-8"
    assert calculate_expression("10 % 3") == "1"


def test_calculator_disallowed_ast_nodes():
    # Calling functions or methods
    assert "Error" in calculate_expression("__import__('os').system('dir')")
    assert "Error" in calculate_expression("print(123)")
    # Bitwise operators or shifts
    assert "Error" in calculate_expression("1 << 5")
    # Lambdas or list comprehensions
    assert "Error" in calculate_expression("[x for x in range(10)]")
    assert "Error" in calculate_expression("lambda x: x + 1")


def test_calculator_zero_division():
    res = calculate_expression("10 / 0")
    assert "Division by zero" in res or "Error" in res


def test_calculator_operand_bounds():
    # Exceeding exponent limits
    res = calculate_expression("2 ** 1001")
    assert "Error" in res
    assert "exponent" in res.lower() or "limit" in res.lower()

    # Exceeding base limits
    res = calculate_expression("10001 ** 2")
    assert "Error" in res
    assert "base" in res.lower() or "limit" in res.lower()


def test_calculator_overflow_handling():
    res = calculate_expression("1e300 ** 2")
    assert "Error" in res



def test_calculator_length_cap():
    long_expr = "1 + " * 100 + "1"  # > 256 characters
    res = calculate_expression(long_expr)
    assert "Error" in res
    assert "length" in res.lower()


def test_calculator_empty_and_garbage_input():
    assert "Error" in calculate_expression("")
    assert "Error" in calculate_expression("    ")
    assert "Error" in calculate_expression("hello world")
    assert "Error" in calculate_expression("2 + + +")


# ============================================================================
# Section 2: File Manager Sandboxing & Security Tests (9 scenarios)
# ============================================================================


def test_file_manager_write_and_read_inside_workspace():
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    try:
        cfg = type("Cfg", (), {"tool_workspace_root": ws_dir})()
        write_res = write_file_content("notes/today.txt", "Meeting notes content", app_settings=cfg)
        assert "Successfully wrote" in write_res

        read_res = read_file_content("notes/today.txt", app_settings=cfg)
        assert read_res == "Meeting notes content"

        list_res = list_files("notes", app_settings=cfg)
        assert "today.txt" in list_res
    finally:
        shutil.rmtree(ws_dir)


def test_file_manager_traversal_rejection():
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    try:
        cfg = type("Cfg", (), {"tool_workspace_root": ws_dir})()
        # Directory traversal with ../
        assert "Error" in write_file_content("../outside.txt", "data", app_settings=cfg)
        assert "Error" in read_file_content("../../etc/passwd", app_settings=cfg)
        assert "Error" in list_files("../../", app_settings=cfg)
        assert "Error" in write_file_content("sub/../../escape.txt", "data", app_settings=cfg)
    finally:
        shutil.rmtree(ws_dir)


def test_file_manager_windows_drive_and_unc_rejection():
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    try:
        cfg = type("Cfg", (), {"tool_workspace_root": ws_dir})()
        assert "Error" in read_file_content("C:/Windows/System32/calc.exe", app_settings=cfg)
        assert "Error" in read_file_content("D:\\data.txt", app_settings=cfg)
        assert "Error" in write_file_content("//remote/share/file.txt", "data", app_settings=cfg)
        assert "Error" in write_file_content("\\\\remote\\share\\file.txt", "data", app_settings=cfg)
    finally:
        shutil.rmtree(ws_dir)


def test_file_manager_blacklisted_extensions():
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    try:
        cfg = type("Cfg", (), {"tool_workspace_root": ws_dir})()
        blacklisted = [".env", ".py", ".db", ".sqlite", ".git", ".key", ".pem", ".exe", ".sh"]
        for ext in blacklisted:
            target = f"test{ext}"
            assert "Error" in write_file_content(target, "secret", app_settings=cfg)
            assert "Error" in read_file_content(target, app_settings=cfg)
    finally:
        shutil.rmtree(ws_dir)


def test_file_manager_size_caps():
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    try:
        cfg = type("Cfg", (), {"tool_workspace_root": ws_dir})()
        # Exceed 100KB write limit
        huge_content = "A" * (100 * 1024 + 1)
        assert "Error" in write_file_content("big.txt", huge_content, app_settings=cfg)

        # Create a large file directly on disk and test read cap
        large_path = Path(ws_dir) / "large.txt"
        large_path.write_text("B" * (100 * 1024 + 50), encoding="utf-8")
        read_res = read_file_content("large.txt", app_settings=cfg)
        assert "Error" in read_res
        assert "exceeds" in read_res.lower() or "limit" in read_res.lower()

    finally:
        shutil.rmtree(ws_dir)


def test_file_manager_symlink_escape_rejection():
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    outside_dir = tempfile.mkdtemp(prefix="edith_outside_")
    try:
        cfg = type("Cfg", (), {"tool_workspace_root": ws_dir})()
        outside_file = Path(outside_dir) / "secret.txt"
        outside_file.write_text("super_secret", encoding="utf-8")

        # Create symlink pointing outside
        link_path = Path(ws_dir) / "leak_link.txt"
        try:
            os.symlink(str(outside_file), str(link_path))
            assert "Error" in read_file_content("leak_link.txt", app_settings=cfg)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not supported on current privilege level")
    finally:
        shutil.rmtree(ws_dir)
        shutil.rmtree(outside_dir)


def test_file_manager_atomic_write_verification():
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    try:
        cfg = type("Cfg", (), {"tool_workspace_root": ws_dir})()
        write_file_content("test_atomic.txt", "Initial content", app_settings=cfg)
        target = Path(ws_dir) / "test_atomic.txt"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "Initial content"

        # Overwrite atomically
        write_file_content("test_atomic.txt", "Updated content", app_settings=cfg)
        assert target.read_text(encoding="utf-8") == "Updated content"
        # No orphan .tmp files left
        tmp_files = list(Path(ws_dir).glob("*.tmp"))
        assert len(tmp_files) == 0
    finally:
        shutil.rmtree(ws_dir)


def test_file_manager_read_nonexistent_file():
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    try:
        cfg = type("Cfg", (), {"tool_workspace_root": ws_dir})()
        res = read_file_content("does_not_exist.txt", app_settings=cfg)
        assert "Error" in res
        assert "not found" in res.lower()
    finally:
        shutil.rmtree(ws_dir)


def test_file_manager_list_directory_clean():
    ws_dir = tempfile.mkdtemp(prefix="edith_ws_")
    try:
        cfg = type("Cfg", (), {"tool_workspace_root": ws_dir})()
        # Empty workspace
        res_empty = list_files(".", app_settings=cfg)
        assert "empty" in res_empty.lower() or "Workspace" in res_empty

        # Populate with items
        write_file_content("file1.txt", "hello", app_settings=cfg)
        write_file_content("subdir/file2.txt", "world", app_settings=cfg)
        res_full = list_files(".", app_settings=cfg)
        assert "file1.txt" in res_full
        assert "subdir" in res_full
    finally:
        shutil.rmtree(ws_dir)


# ============================================================================
# Section 3: Network Policy & Sanitization Tests (5 scenarios)
# ============================================================================


def test_network_tools_policy_disabled_by_default():
    cfg = type("Cfg", (), {"allow_external_network_tools": False})()
    res_search = search_web("python tutorials", app_settings=cfg)
    assert "Error" in res_search
    assert "disabled" in res_search.lower()

    res_weather = get_weather("Tokyo", app_settings=cfg)
    assert "Error" in res_weather
    assert "disabled" in res_weather.lower()


def test_network_tools_search_input_validation():
    cfg = type("Cfg", (), {"allow_external_network_tools": True, "tool_timeout_seconds": 1.0})()
    assert "Error" in search_web("", app_settings=cfg)
    assert "Error" in search_web("   ", app_settings=cfg)


def test_network_tools_weather_input_validation():
    cfg = type("Cfg", (), {"allow_external_network_tools": True, "tool_timeout_seconds": 1.0})()
    assert "Error" in get_weather("", app_settings=cfg)
    assert "Error" in get_weather("   ", app_settings=cfg)


@patch("httpx.Client.get")
def test_network_tools_search_timeout_sanitized(mock_get):
    mock_get.side_effect = httpx.TimeoutException("Connection timed out")
    cfg = type("Cfg", (), {"allow_external_network_tools": True, "tool_timeout_seconds": 1.0})()
    res = search_web("ai safety", app_settings=cfg)
    assert "Error" in res
    assert "timed out" in res.lower()
    # Does not leak raw exception trace
    assert "Traceback" not in res


@patch("httpx.Client.get")
def test_network_tools_weather_http_error_sanitized(mock_get):
    req = httpx.Request("GET", "https://wttr.in/Paris")
    mock_get.side_effect = httpx.HTTPStatusError("Service Unavailable", request=req, response=httpx.Response(503, request=req))
    cfg = type("Cfg", (), {"allow_external_network_tools": True, "tool_timeout_seconds": 1.0})()
    res = get_weather("Paris", app_settings=cfg)
    assert "Error" in res
    assert "error" in res.lower()
    assert "Traceback" not in res


# ============================================================================
# Section 4: Tool Registry Schema, Aliasing & Validation Tests (6 scenarios)
# ============================================================================


def test_tool_registry_default_tools_and_aliases():
    registry = ToolRegistry()
    tools = registry.list_tools()
    assert "calculator" in tools
    assert "search" in tools
    assert "weather" in tools
    assert "file_manager:list" in tools or "file_manager_list" in tools
    assert "file_manager:read" in tools or "file_manager_read" in tools
    assert "file_manager:write" in tools or "file_manager_write" in tools
    assert "youtube" in tools
    assert "minecraft" in tools

    # Both colon and underscore aliases resolve to the same tool definition
    tool_col = registry.get_tool("file_manager:read")
    tool_und = registry.get_tool("file_manager_read")
    assert tool_col is not None
    assert tool_col == tool_und


def test_tool_registry_ollama_schema_format():
    registry = ToolRegistry()
    schemas = registry.get_ollama_tools()
    assert isinstance(schemas, list)
    assert len(schemas) >= 8

    calc_schema = next(s for s in schemas if s["function"]["name"] == "calculator")
    assert calc_schema["type"] == "function"
    assert "expression" in calc_schema["function"]["parameters"]["properties"]
    assert "required" in calc_schema["function"]["parameters"]


def test_tool_registry_argument_validation():
    registry = ToolRegistry()
    calc = registry.get_tool("calculator")
    assert calc is not None

    # Valid arguments
    valid, err = registry.validate_arguments(calc, {"expression": "1 + 1"})
    assert valid is True
    assert err is None

    # Missing required argument
    valid, err = registry.validate_arguments(calc, {})
    assert valid is False
    assert "Missing required parameter" in err

    # Wrong type
    valid, err = registry.validate_arguments(calc, {"expression": 12345})
    assert valid is False
    assert "must be a string" in err


@pytest.mark.asyncio
async def test_tool_registry_async_execution():
    registry = ToolRegistry()
    res = await registry.execute_tool("calculator", {"expression": "100 / 4"})
    assert res.success is True
    assert res.output == "25"


@pytest.mark.asyncio
async def test_tool_registry_unknown_tool_error():
    registry = ToolRegistry()
    res = await registry.execute_tool("unknown_super_tool", {"arg": "val"})
    assert res.success is False
    assert "not recognized" in res.output.lower() or "not recognized" in (res.error or "").lower()


@pytest.mark.asyncio
async def test_tool_registry_output_truncation():
    # Register a test tool that produces very large output
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="test_large_output",
            description="Outputs large text",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "X" * 5000,
            permission=PermissionCategory.READ_ONLY,
        )
    )
    res = await registry.execute_tool("test_large_output", {}, max_output_chars=100)
    assert len(res.output) < 200
    assert "[Output truncated" in res.output


# ============================================================================
# Section 5: Ollama Client Chat & Capability Fallback Tests (4 scenarios)
# ============================================================================


@pytest.mark.asyncio
async def test_client_chat_with_tools_payload():
    client = OllamaClient()
    mock_resp = httpx.Response(
        200,
        json={
            "model": "qwen3:8b",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "calculator",
                            "arguments": {"expression": "25 * 4"},
                        }
                    }
                ],
            },
        },
        request=httpx.Request("POST", "http://test/api/chat"),
    )

    with patch.object(client.session, "post", AsyncMock(return_value=mock_resp)) as mock_post:
        resp = await client.chat(
            messages=[{"role": "user", "content": "What is 25 * 4?"}],
            tools=[{"type": "function", "function": {"name": "calculator"}}],
            validate=False,
        )
        assert len(resp["tool_calls"]) == 1
        assert resp["tool_calls"][0]["name"] == "calculator"
        assert resp["tool_calls"][0]["arguments"] == {"expression": "25 * 4"}
        mock_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_chat_string_arguments_parsing():
    client = OllamaClient()
    mock_resp = httpx.Response(
        200,
        json={
            "model": "qwen3:8b",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression": "7 * 7"}',  # JSON encoded string
                        }
                    }
                ],
            },
        },
        request=httpx.Request("POST", "http://test/api/chat"),
    )

    with patch.object(client.session, "post", AsyncMock(return_value=mock_resp)):
        resp = await client.chat(
            messages=[{"role": "user", "content": "Calculate 7*7"}],
            validate=False,
        )
        assert resp["tool_calls"][0]["arguments"] == {"expression": "7 * 7"}


@pytest.mark.asyncio
async def test_client_chat_tool_unsupported_model_fallback():
    client = OllamaClient()
    # First response: 400 Bad Request with error stating tools are not supported
    err_resp = httpx.Response(
        400,
        text="error: model 'mistral:7b' does not support tools",
        request=httpx.Request("POST", "http://test/api/chat"),
    )
    # Second fallback response without tools: 200 OK
    ok_resp = httpx.Response(
        200,
        json={
            "model": "mistral:7b",
            "message": {"role": "assistant", "content": "I am answering without tools directly."},
        },
        request=httpx.Request("POST", "http://test/api/chat"),
    )

    with patch.object(client.session, "post", AsyncMock(side_effect=[err_resp, ok_resp])) as mock_post:
        resp = await client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            model="mistral:7b",
            tools=[{"type": "function", "function": {"name": "calculator"}}],
            validate=False,
        )
        assert resp["content"] == "I am answering without tools directly."
        assert not client.is_tool_supported("mistral:7b")
        assert mock_post.await_count == 2
        # Second call stripped tools
        second_call_kwargs = mock_post.await_args_list[1][1]
        assert "tools" not in second_call_kwargs["json"]


def test_client_tool_support_tracking():
    client = OllamaClient()
    assert client.is_tool_supported("qwen3:8b") is True
    client.mark_tool_unsupported("qwen3:8b")
    assert client.is_tool_supported("qwen3:8b") is False


# ============================================================================
# Section 6: Tool Orchestrator Loops, Limits & Framing Tests (7 scenarios)
# ============================================================================


@pytest.mark.asyncio
async def test_orchestrator_single_turn_no_tools():
    client = OllamaClient()
    orchestrator = ToolOrchestrator(client)

    client.chat = AsyncMock(return_value={"content": "Direct text response.", "tool_calls": []})
    result = await orchestrator.run_turn(
        user_id=1,
        conversation_id=1,
        user_message="Hello",
        system_context="You are EDITH.",
    )
    assert result == "Direct text response."


@pytest.mark.asyncio
async def test_orchestrator_single_tool_execution_flow():
    client = OllamaClient()
    registry = ToolRegistry()
    orchestrator = ToolOrchestrator(client, registry)

    # Turn 1: Model asks to call calculator
    turn1_resp = {
        "content": "",
        "tool_calls": [{"name": "calculator", "arguments": {"expression": "12 * 12"}}],
    }
    # Turn 2: Model receives tool result and provides final synthesis
    turn2_resp = {
        "content": "The result of 12 * 12 is 144.",
        "tool_calls": [],
    }

    client.chat = AsyncMock(side_effect=[turn1_resp, turn2_resp])
    result = await orchestrator.run_turn(
        user_id=1,
        conversation_id=1,
        user_message="What is 12 * 12?",
        system_context="You are EDITH.",
    )
    assert "144" in result
    assert client.chat.await_count == 2


@pytest.mark.asyncio
async def test_orchestrator_max_rounds_bound():
    client = OllamaClient()
    registry = ToolRegistry()
    cfg = type("Cfg", (), {
        "enable_tool_execution": True,
        "tool_max_rounds": 2,
        "tool_max_calls_per_request": 10,
        "tool_timeout_seconds": 5.0,
        "tool_max_output_chars": 1000,
    })()
    orchestrator = ToolOrchestrator(client, registry, cfg)

    # Endless tool call responses
    tool_resp = {
        "content": "",
        "tool_calls": [{"name": "calculator", "arguments": {"expression": "1 + 1"}}],
    }
    final_resp = {
        "content": "Final synthesized answer after max rounds reached.",
        "tool_calls": [],
    }

    # Model requests tool on round 1 and round 2, then final synthesis is requested
    client.chat = AsyncMock(side_effect=[tool_resp, tool_resp, final_resp])
    res = await orchestrator.run_turn(
        user_id=1,
        conversation_id=1,
        user_message="Looping test",
        system_context="You are EDITH.",
    )
    assert res == "Final synthesized answer after max rounds reached."
    assert client.chat.await_count == 3  # 2 rounds + 1 final synthesis


@pytest.mark.asyncio
async def test_orchestrator_max_calls_bound():
    client = OllamaClient()
    registry = ToolRegistry()
    cfg = type("Cfg", (), {
        "enable_tool_execution": True,
        "tool_max_rounds": 3,
        "tool_max_calls_per_request": 2,  # Maximum 2 calls allowed
        "tool_timeout_seconds": 5.0,
        "tool_max_output_chars": 1000,
    })()
    orchestrator = ToolOrchestrator(client, registry, cfg)

    # Model requests 3 tool calls in a single turn
    resp1 = {
        "content": "",
        "tool_calls": [
            {"name": "calculator", "arguments": {"expression": "1+1"}},
            {"name": "calculator", "arguments": {"expression": "2+2"}},
            {"name": "calculator", "arguments": {"expression": "3+3"}},
        ],
    }
    resp2 = {"content": "Handled calls with cap.", "tool_calls": []}

    client.chat = AsyncMock(side_effect=[resp1, resp2])
    res = await orchestrator.run_turn(
        user_id=1,
        conversation_id=1,
        user_message="3 calls",
        system_context="You are EDITH.",
    )
    assert res == "Handled calls with cap."
    # Check messages sent on round 2: the 3rd tool call should receive an aborted error
    second_chat_messages = client.chat.await_args_list[1][1]["messages"]
    tool_msgs = [m for m in second_chat_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert "aborted" in tool_msgs[2]["content"].lower() or "limit" in tool_msgs[2]["content"].lower()


@pytest.mark.asyncio
async def test_orchestrator_untrusted_framing():
    client = OllamaClient()
    registry = ToolRegistry()
    orchestrator = ToolOrchestrator(client, registry)

    tool_resp = {
        "content": "",
        "tool_calls": [{"name": "calculator", "arguments": {"expression": "10 + 20"}}],
    }
    final_resp = {"content": "Answer is 30.", "tool_calls": []}

    client.chat = AsyncMock(side_effect=[tool_resp, final_resp])
    await orchestrator.run_turn(
        user_id=1,
        conversation_id=1,
        user_message="Calculate",
        system_context="You are EDITH.",
    )

    second_call_messages = client.chat.await_args_list[1][1]["messages"]
    tool_msg = next(m for m in second_call_messages if m.get("role") == "tool")
    assert "Untrusted reference" in tool_msg["content"] or "External reference" in tool_msg["content"]


@pytest.mark.asyncio
async def test_orchestrator_staging_confirmation_on_sensitive_tool():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        user = await get_or_create_user("test_user_staging", "Tester")
        conv = await create_conversation(user.id, "chat_stage")

        client = OllamaClient()
        registry = ToolRegistry(settings)
        orchestrator = ToolOrchestrator(client, registry, settings)

        # Model requests sensitive file write
        tool_resp = {
            "content": "",
            "tool_calls": [{
                "name": "file_manager_write",
                "arguments": {"file_path": "diary.txt", "content": "Secret entry"},
            }],
        }
        client.chat = AsyncMock(return_value=tool_resp)

        reply = await orchestrator.run_turn(
            user_id=user.id,
            conversation_id=conv.id,
            user_message="Write to diary.txt",
            system_context="You are EDITH.",
        )

        assert "Action Requires Confirmation" in reply
        assert "/confirm" in reply
        assert "/cancel" in reply
        assert "diary.txt" in reply

        # Verify PendingAction was staged in database
        active_action = await get_active_pending_action(user.id, conv.id)
        assert active_action is not None
        assert active_action.tool_name == "file_manager_write"
        assert active_action.status == "PENDING"
        assert active_action.confirmation_token in reply


@pytest.mark.asyncio
async def test_orchestrator_tools_disabled_policy():
    client = OllamaClient()
    registry = ToolRegistry()
    cfg = type("Cfg", (), {"enable_tool_execution": False, "tool_max_rounds": 1, "tool_max_calls_per_request": 5})()
    orchestrator = ToolOrchestrator(client, registry, cfg)

    client.chat = AsyncMock(return_value={"content": "Direct chat without tools.", "tool_calls": []})
    await orchestrator.run_turn(
        user_id=1,
        conversation_id=1,
        user_message="Hello",
        system_context="You are EDITH.",
    )
    # tools payload should be None
    called_tools = client.chat.await_args_list[0][1].get("tools")
    assert called_tools is None


# ============================================================================
# Section 7: Confirmation Flow & /confirm /cancel Commands (8 scenarios)
# ============================================================================


@pytest.mark.asyncio
async def test_confirm_command_with_valid_token():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        user = await get_or_create_user("test_user_confirm", "Tester")
        conv = await create_conversation(user.id, "chat_confirm")
        registry = ToolRegistry(settings)

        # Stage a pending action manually
        action = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "approved.txt", "content": "Approved Content"},
            summary="Write file approved.txt",
        )

        is_cmd, reply = await handle_user_command(
            user=user,
            text=f"/confirm {action.confirmation_token}",
            ai_client=AsyncMock(),
            settings=settings,
            conversation_id=conv.id,
            tool_registry=registry,
        )

        assert is_cmd is True
        assert "Action Confirmed and Executed" in reply

        # File was actually created
        ws_file = Path(env["ws_dir"]) / "approved.txt"
        assert ws_file.exists()
        assert ws_file.read_text(encoding="utf-8") == "Approved Content"

        # Action status changed to CONSUMED
        consumed = await get_active_pending_action(user.id, conv.id)
        assert consumed is None


@pytest.mark.asyncio
async def test_confirm_command_with_yes():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        user = await get_or_create_user("test_user_yes", "Tester")
        conv = await create_conversation(user.id, "chat_yes")
        registry = ToolRegistry(settings)

        await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "yes.txt", "content": "Yes content"},
            summary="Write file yes.txt",
        )

        is_cmd, reply = await handle_user_command(
            user=user,
            text="/confirm yes",
            ai_client=AsyncMock(),
            settings=settings,
            conversation_id=conv.id,
            tool_registry=registry,
        )

        assert is_cmd is True
        assert "Action Confirmed and Executed" in reply
        assert (Path(env["ws_dir"]) / "yes.txt").exists()


@pytest.mark.asyncio
async def test_confirm_command_invalid_token():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        user = await get_or_create_user("test_user_invalid", "Tester")
        conv = await create_conversation(user.id, "chat_invalid")

        await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "invalid.txt", "content": "test"},
            summary="Write invalid.txt",
        )

        is_cmd, reply = await handle_user_command(
            user=user,
            text="/confirm wrong_token",
            ai_client=AsyncMock(),
            settings=settings,
            conversation_id=conv.id,
        )

        assert is_cmd is True
        assert "Invalid confirmation token" in reply
        # Action remains pending
        active = await get_active_pending_action(user.id, conv.id)
        assert active is not None


@pytest.mark.asyncio
async def test_confirm_command_no_pending_action():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        user = await get_or_create_user("test_user_none", "Tester")
        conv = await create_conversation(user.id, "chat_none")

        is_cmd, reply = await handle_user_command(
            user=user,
            text="/confirm",
            ai_client=AsyncMock(),
            settings=settings,
            conversation_id=conv.id,
        )
        assert is_cmd is True
        assert "No pending action requires confirmation" in reply


@pytest.mark.asyncio
async def test_confirm_ttl_expiration():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        user = await get_or_create_user("test_user_ttl", "Tester")
        conv = await create_conversation(user.id, "chat_ttl")

        # Staged with 0 second TTL (already expired)
        action = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "ttl.txt", "content": "expired"},
            summary="Write ttl.txt",
            ttl_seconds=-1,
        )

        is_cmd, reply = await handle_user_command(
            user=user,
            text=f"/confirm {action.confirmation_token}",
            ai_client=AsyncMock(),
            settings=settings,
            conversation_id=conv.id,
        )
        assert is_cmd is True
        assert "No pending action requires confirmation" in reply or "expired" in reply.lower()


@pytest.mark.asyncio
async def test_confirm_double_execution_prevention():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        user = await get_or_create_user("test_user_double", "Tester")
        conv = await create_conversation(user.id, "chat_double")
        registry = ToolRegistry(settings)

        action = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "double.txt", "content": "double"},
            summary="Write double.txt",
        )

        # First confirm succeeds
        is_cmd1, reply1 = await handle_user_command(
            user=user,
            text=f"/confirm {action.confirmation_token}",
            ai_client=AsyncMock(),
            settings=settings,
            conversation_id=conv.id,
            tool_registry=registry,
        )
        assert "Action Confirmed and Executed" in reply1

        # Second confirm fails idempotently
        is_cmd2, reply2 = await handle_user_command(
            user=user,
            text=f"/confirm {action.confirmation_token}",
            ai_client=AsyncMock(),
            settings=settings,
            conversation_id=conv.id,
            tool_registry=registry,
        )
        assert "No pending action requires confirmation" in reply2 or "already been processed" in reply2


@pytest.mark.asyncio
async def test_cancel_command_success():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        user = await get_or_create_user("test_user_cancel", "Tester")
        conv = await create_conversation(user.id, "chat_cancel")

        await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "cancel.txt", "content": "cancel me"},
            summary="Write cancel.txt",
        )

        is_cmd, reply = await handle_user_command(
            user=user,
            text="/cancel",
            ai_client=AsyncMock(),
            settings=settings,
            conversation_id=conv.id,
        )
        assert is_cmd is True
        assert "Action Cancelled" in reply

        # Subsequent confirm fails
        active = await get_active_pending_action(user.id, conv.id)
        assert active is None


@pytest.mark.asyncio
async def test_conflicting_pending_action_cancels_previous():
    async with create_isolated_tool_env() as env:
        user = await get_or_create_user("test_user_conflict", "Tester")
        conv = await create_conversation(user.id, "chat_conflict")

        action1 = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "first.txt", "content": "1"},
            summary="Write first.txt",
        )
        # Create second action
        action2 = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "second.txt", "content": "2"},
            summary="Write second.txt",
        )

        active = await get_active_pending_action(user.id, conv.id)
        assert active is not None
        assert active.id == action2.id
        assert active.confirmation_token == action2.confirmation_token


# ============================================================================
# Section 8: Gateway Integration Tests (4 scenarios)
# ============================================================================


@pytest.mark.asyncio
async def test_whatsapp_gateway_tool_execution_flow():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        from whatsapp.service import WhatsAppGateway
        from ai.manager import AIManager

        ai_mgr = AIManager(env["settings"].storage_path)
        gateway = WhatsAppGateway(settings, ai_mgr)

        # Mock OllamaClient chat to execute calculator
        mock_client = OllamaClient()
        mock_client.chat = AsyncMock(side_effect=[
            {"content": "", "tool_calls": [{"name": "calculator", "arguments": {"expression": "50 + 50"}}]},
            {"content": "50 + 50 is 100.", "tool_calls": []},
        ])
        mock_client.get_installed_models = AsyncMock(return_value=[type("M", (), {"name": "qwen3:8b"})()])
        mock_client.is_ollama_available = AsyncMock(return_value=True)
        gateway.ai_client = mock_client

        record = type("InboundRecord", (), {
            "id": 999,
            "sender": "12345@s.whatsapp.net",
            "sender_name": "TestUser",
            "chat_id": "12345@s.whatsapp.net",
            "content": "What is 50 + 50?",
            "message_type": "conversation",
            "status": "RECEIVED",
            "response_text": None,
            "external_id": "ext_999",
        })()

        gateway.send_typing = AsyncMock()
        gateway.send_message = AsyncMock()

        await gateway._process_inbound_record(record)
        gateway.send_message.assert_awaited_once()
        sent_text = gateway.send_message.await_args[0][1]
        assert "100" in sent_text


@pytest.mark.asyncio
async def test_whatsapp_gateway_confirmation_flow():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        from whatsapp.service import WhatsAppGateway
        from ai.manager import AIManager

        ai_mgr = AIManager(env["settings"].storage_path)
        gateway = WhatsAppGateway(settings, ai_mgr)

        mock_client = OllamaClient()
        mock_client.chat = AsyncMock(return_value={
            "content": "",
            "tool_calls": [{
                "name": "file_manager_write",
                "arguments": {"file_path": "wa_test.txt", "content": "WhatsApp content"},
            }],
        })
        mock_client.get_installed_models = AsyncMock(return_value=[type("M", (), {"name": "qwen3:8b"})()])
        mock_client.is_ollama_available = AsyncMock(return_value=True)
        gateway.ai_client = mock_client
        gateway.send_typing = AsyncMock()
        gateway.send_message = AsyncMock()

        record = type("InboundRecord", (), {
            "id": 1001,
            "sender": "wa_user@s.whatsapp.net",
            "sender_name": "WAUser",
            "chat_id": "wa_user@s.whatsapp.net",
            "content": "Write wa_test.txt",
            "message_type": "conversation",
            "status": "RECEIVED",
            "response_text": None,
            "external_id": "ext_1001",
        })()

        # Step 1: Processing generates confirmation request
        await gateway._process_inbound_record(record)
        gateway.send_message.assert_awaited_once()
        conf_prompt = gateway.send_message.await_args[0][1]
        assert "Action Requires Confirmation" in conf_prompt

        # Step 2: Confirm message arrives
        record_confirm = type("InboundRecord", (), {
            "id": 1002,
            "sender": "wa_user@s.whatsapp.net",
            "sender_name": "WAUser",
            "chat_id": "wa_user@s.whatsapp.net",
            "content": "/confirm yes",
            "message_type": "conversation",
            "status": "RECEIVED",
            "response_text": None,
            "external_id": "ext_1002",
        })()

        await gateway._process_inbound_record(record_confirm)
        assert gateway.send_message.await_count == 2
        exec_reply = gateway.send_message.await_args_list[1][0][1]
        assert "Action Confirmed and Executed" in exec_reply
        assert (Path(env["ws_dir"]) / "wa_test.txt").exists()


@pytest.mark.asyncio
async def test_telegram_gateway_tool_execution():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        settings.telegram_bot_token = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789"
        from telegram_gateway.bot import TelegramGateway
        from ai.manager import AIManager

        ai_mgr = AIManager(env["settings"].storage_path)
        gateway = TelegramGateway(settings, None, ai_mgr)

        mock_client = OllamaClient()
        mock_client.chat = AsyncMock(side_effect=[
            {"content": "", "tool_calls": [{"name": "calculator", "arguments": {"expression": "8 * 8"}}]},
            {"content": "8 * 8 is 64.", "tool_calls": []},
        ])
        mock_client.get_installed_models = AsyncMock(return_value=[type("M", (), {"name": "qwen3:8b"})()])
        mock_client.is_ollama_available = AsyncMock(return_value=True)
        gateway.ai_client = mock_client
        gateway.tool_orchestrator.ai_client = mock_client


        update = MagicMock()
        update.effective_chat.id = 55555
        update.effective_chat.title = "TG Chat"
        update.effective_user.id = 99999
        update.effective_user.username = "tg_tester"
        update.effective_user.full_name = "TG Tester"
        update.message.text = "Calculate 8*8"
        update.message.reply_text = AsyncMock()

        await gateway.handle_message(update, None)
        update.message.reply_text.assert_awaited_once()
        reply_text = update.message.reply_text.await_args[0][0]
        assert "64" in reply_text


@pytest.mark.asyncio
async def test_telegram_gateway_confirmation_flow():
    async with create_isolated_tool_env() as env:
        settings = env["settings"]
        settings.telegram_bot_token = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789"
        from telegram_gateway.bot import TelegramGateway
        from ai.manager import AIManager

        ai_mgr = AIManager(env["settings"].storage_path)
        gateway = TelegramGateway(settings, None, ai_mgr)

        mock_client = OllamaClient()
        mock_client.chat = AsyncMock(return_value={
            "content": "",
            "tool_calls": [{
                "name": "file_manager_write",
                "arguments": {"file_path": "tg_note.txt", "content": "TG note"},
            }],
        })
        mock_client.get_installed_models = AsyncMock(return_value=[type("M", (), {"name": "qwen3:8b"})()])
        mock_client.is_ollama_available = AsyncMock(return_value=True)
        gateway.ai_client = mock_client
        gateway.tool_orchestrator.ai_client = mock_client


        update1 = MagicMock()
        update1.effective_chat.id = 77777
        update1.effective_chat.title = "TG Group"
        update1.effective_user.id = 88888
        update1.effective_user.username = "tg_writer"
        update1.effective_user.full_name = "TG Writer"
        update1.message.text = "Write tg_note.txt"
        update1.message.reply_text = AsyncMock()

        # Step 1: Action requires confirmation
        await gateway.handle_message(update1, None)
        conf_text = update1.message.reply_text.await_args[0][0]
        assert "Action Requires Confirmation" in conf_text

        # Step 2: User confirms
        update2 = MagicMock()
        update2.effective_chat.id = 77777
        update2.effective_chat.title = "TG Group"
        update2.effective_user.id = 88888
        update2.effective_user.username = "tg_writer"
        update2.effective_user.full_name = "TG Writer"
        update2.message.text = "/confirm yes"
        update2.message.reply_text = AsyncMock()

        await gateway.handle_message(update2, None)
        exec_text = update2.message.reply_text.await_args[0][0]
        assert "Action Confirmed and Executed" in exec_text
        assert (Path(env["ws_dir"]) / "tg_note.txt").exists()


# ============================================================================
# Phase 4B.1: Security Closeout Regression Tests
# ============================================================================

@pytest.mark.asyncio
async def test_token_entropy_128_bits():
    """Verify confirmation tokens have at least 128 bits of entropy (32 hex characters)."""
    async with create_isolated_tool_env() as env:
        user = await get_or_create_user("whatsapp", "entropy_user")
        conv = await create_conversation(user.id, "whatsapp", "entropy_user")

        action = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "entropy.txt", "content": "128bit"},
            summary="Entropy test",
        )
        # 16 bytes = 128 bits = 32 hexadecimal characters
        assert len(action.confirmation_token) == 32
        int(action.confirmation_token, 16)  # must be valid hex

        # Check randomness/uniqueness across multiple actions
        action2 = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "entropy2.txt", "content": "128bit"},
            summary="Entropy test 2",
        )
        assert action2.confirmation_token != action.confirmation_token
        assert len(action2.confirmation_token) == 32


@pytest.mark.asyncio
async def test_wrong_user_cannot_confirm_pending_action():
    """Verify that User B cannot confirm or consume User A's pending action."""
    async with create_isolated_tool_env() as env:
        user_a = await get_or_create_user("whatsapp", "victim_a")
        conv_a = await create_conversation(user_a.id, "whatsapp", "victim_a")

        user_b = await get_or_create_user("whatsapp", "attacker_b")
        conv_b = await create_conversation(user_b.id, "whatsapp", "attacker_b")

        action = await create_pending_action(
            user_id=user_a.id,
            conversation_id=conv_a.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "secret_file.txt", "content": "confidential"},
            summary="Write confidential file",
        )

        registry = ToolRegistry(env["settings"])

        # User B attempts to confirm with 'yes'
        is_cmd, reply = await handle_user_command(
            user_b,
            "/confirm yes",
            None,
            env["settings"],
            None,
            conversation_id=conv_b.id,
            tool_registry=registry,
        )
        assert is_cmd is True
        assert "No pending action requires confirmation" in reply

        # User B attempts to confirm with User A's exact token
        is_cmd, reply = await handle_user_command(
            user_b,
            f"/confirm {action.confirmation_token}",
            None,
            env["settings"],
            None,
            conversation_id=conv_b.id,
            tool_registry=registry,
        )
        assert is_cmd is True
        assert "No pending action requires confirmation" in reply

        # Ensure file was NOT created
        assert not (Path(env["ws_dir"]) / "secret_file.txt").exists()


@pytest.mark.asyncio
async def test_wrong_conversation_cannot_confirm_pending_action():
    """Verify that an action staged in Conversation A cannot be confirmed from Conversation B."""
    async with create_isolated_tool_env() as env:
        user = await get_or_create_user("whatsapp", "multi_conv_user")
        conv_1 = await create_conversation(user.id, "whatsapp", "chat_1")
        conv_2 = await create_conversation(user.id, "whatsapp", "chat_2")

        action = await create_pending_action(
            user_id=user.id,
            conversation_id=conv_1.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "chat1.txt", "content": "chat 1 content"},
            summary="Write chat1 file",
        )

        registry = ToolRegistry(env["settings"])

        # Try to confirm from conv_2
        is_cmd, reply = await handle_user_command(
            user,
            "/confirm yes",
            None,
            env["settings"],
            None,
            conversation_id=conv_2.id,
            tool_registry=registry,
        )
        assert is_cmd is True
        assert "No pending action requires confirmation" in reply
        assert not (Path(env["ws_dir"]) / "chat1.txt").exists()


@pytest.mark.asyncio
async def test_expired_pending_action_rejection():
    """Verify that expired actions cannot be confirmed or consumed."""
    async with create_isolated_tool_env() as env:
        user = await get_or_create_user("whatsapp", "expiry_user")
        conv = await create_conversation(user.id, "whatsapp", "expiry_user")

        # Create action with 0.05s TTL
        action = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "expired.txt", "content": "too late"},
            summary="Write expired file",
            ttl_seconds=0.05,
        )

        await asyncio.sleep(0.1)

        registry = ToolRegistry(env["settings"])
        is_cmd, reply = await handle_user_command(
            user,
            f"/confirm {action.confirmation_token}",
            None,
            env["settings"],
            None,
            conversation_id=conv.id,
            tool_registry=registry,
        )
        assert is_cmd is True
        assert "expired" in reply.lower()
        assert not (Path(env["ws_dir"]) / "expired.txt").exists()


@pytest.mark.asyncio
async def test_atomic_consumption_replay_and_duplicate_commands():
    """Verify replay protection and atomic consumption prevent duplicate execution."""
    async with create_isolated_tool_env() as env:
        user = await get_or_create_user("whatsapp", "replay_user")
        conv = await create_conversation(user.id, "whatsapp", "replay_user")

        action = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "replay.txt", "content": "replay test"},
            summary="Write replay file",
        )

        # Direct atomic consumption
        res1 = await consume_pending_action(action.id, user_id=user.id, conversation_id=conv.id)
        res2 = await consume_pending_action(action.id, user_id=user.id, conversation_id=conv.id)
        assert res1 is True
        assert res2 is False  # Replay blocked atomically!

        # Concurrent atomic consumption test
        action_concurrent = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "concurrent.txt", "content": "concurrent test"},
            summary="Write concurrent file",
        )

        c_results = await asyncio.gather(
            consume_pending_action(action_concurrent.id, user_id=user.id, conversation_id=conv.id),
            consume_pending_action(action_concurrent.id, user_id=user.id, conversation_id=conv.id),
            consume_pending_action(action_concurrent.id, user_id=user.id, conversation_id=conv.id),
        )
        # Exactly one must succeed
        assert c_results.count(True) == 1
        assert c_results.count(False) == 2


@pytest.mark.asyncio
async def test_excessive_failed_attempts_invalidation():
    """Verify that entering incorrect tokens 3 times cancels the pending action."""
    async with create_isolated_tool_env() as env:
        user = await get_or_create_user("whatsapp", "brute_force_user")
        conv = await create_conversation(user.id, "whatsapp", "brute_force_user")

        action = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "brute.txt", "content": "brute test"},
            summary="Write brute file",
        )

        registry = ToolRegistry(env["settings"])

        # Attempt 1: wrong token
        is_cmd, reply1 = await handle_user_command(
            user,
            "/confirm badtoken1",
            None,
            env["settings"],
            None,
            conversation_id=conv.id,
            tool_registry=registry,
        )
        assert is_cmd is True
        assert "Invalid confirmation token (2 attempts remaining)" in reply1
        assert action.confirmation_token not in reply1

        # Attempt 2: wrong token
        is_cmd, reply2 = await handle_user_command(
            user,
            "/confirm badtoken2",
            None,
            env["settings"],
            None,
            conversation_id=conv.id,
            tool_registry=registry,
        )
        assert is_cmd is True
        assert "Invalid confirmation token (1 attempt remaining)" in reply2
        assert action.confirmation_token not in reply2

        # Attempt 3: wrong token - should cancel
        is_cmd, reply3 = await handle_user_command(
            user,
            "/confirm badtoken3",
            None,
            env["settings"],
            None,
            conversation_id=conv.id,
            tool_registry=registry,
        )
        assert is_cmd is True
        assert "maximum attempt limit reached" in reply3
        assert action.confirmation_token not in reply3

        # Attempt 4: even with correct token, it must fail because status is CANCELLED
        is_cmd, reply4 = await handle_user_command(
            user,
            f"/confirm {action.confirmation_token}",
            None,
            env["settings"],
            None,
            conversation_id=conv.id,
            tool_registry=registry,
        )
        assert is_cmd is True
        assert "No pending action requires confirmation" in reply4
        assert not (Path(env["ws_dir"]) / "brute.txt").exists()


@pytest.mark.asyncio
async def test_bare_confirm_describes_action_and_yes_authorizes():
    """Verify that bare /confirm describes the action without authorizing, and /confirm yes authorizes."""
    async with create_isolated_tool_env() as env:
        user = await get_or_create_user("whatsapp", "desc_user")
        conv = await create_conversation(user.id, "whatsapp", "desc_user")

        action = await create_pending_action(
            user_id=user.id,
            conversation_id=conv.id,
            action_type="tool_execution",
            tool_name="file_manager_write",
            arguments={"file_path": "desc.txt", "content": "desc content"},
            summary="Write desc.txt (12 characters)",
        )

        registry = ToolRegistry(env["settings"])

        # Send bare /confirm
        is_cmd, reply = await handle_user_command(
            user,
            "/confirm",
            None,
            env["settings"],
            None,
            conversation_id=conv.id,
            tool_registry=registry,
        )
        assert is_cmd is True
        assert "Confirmation Required" in reply
        assert "Write desc.txt" in reply
        assert not (Path(env["ws_dir"]) / "desc.txt").exists()  # Not executed yet!

        # Now send /confirm yes
        is_cmd2, reply2 = await handle_user_command(
            user,
            "/confirm yes",
            None,
            env["settings"],
            None,
            conversation_id=conv.id,
            tool_registry=registry,
        )
        assert is_cmd2 is True
        assert "Action Confirmed and Executed" in reply2
        assert (Path(env["ws_dir"]) / "desc.txt").exists()


def test_external_network_tools_default_disabled_no_network_calls():
    """Verify that external-network tools make zero network calls when disabled by default."""
    from config import Settings
    default_cfg = Settings()
    assert default_cfg.allow_external_network_tools is False

    with patch("httpx.Client") as mock_client:
        search_res = search_web("python documentation", app_settings=default_cfg)
        assert "disabled by default for privacy" in search_res
        mock_client.assert_not_called()

    with patch("httpx.Client") as mock_client:
        weather_res = get_weather("London", app_settings=default_cfg)
        assert "disabled by default for privacy" in weather_res
        mock_client.assert_not_called()


def test_external_network_tools_when_explicitly_enabled():
    """Verify that external-network tools make requests when explicitly enabled."""
    from config import Settings
    cfg = Settings(allow_external_network_tools=True)
    assert cfg.allow_external_network_tools is True

    with patch("httpx.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"AbstractText": "Python is a programming language."}
        mock_instance.get.return_value = mock_resp
        mock_client_cls.return_value.__enter__.return_value = mock_instance

        res = search_web("Python", app_settings=cfg)
        assert "Python is a programming language" in res
        mock_instance.get.assert_called_once()


def test_file_manager_windows_reserved_device_names():
    """Verify that Windows reserved device names are strictly rejected."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        reserved_examples = ["CON", "prn", "AUX", "NUL", "com1", "lpt1", "con.txt", "sub/aux.log"]
        for name in reserved_examples:
            with pytest.raises(SafePathError) as exc_info:
                resolve_safe_path(name, workspace_root=ws)
            assert "Reserved device name" in str(exc_info.value)


def test_file_manager_prefix_collision_prevention():
    """Verify that path prefix collisions outside workspace root are rejected."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "workspace"
        ws.mkdir()
        evil_ws = Path(td) / "workspace_evil"
        evil_ws.mkdir()

        # Attempt to target evil_ws relative or via ../
        with pytest.raises(SafePathError):
            resolve_safe_path("../workspace_evil/hack.txt", workspace_root=ws)


def test_file_manager_protected_components_in_subdirectories():
    """Verify that protected names and dotfiles are rejected at any subdirectory depth."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        blocked = ["sub/.env", "nested/.secret", "deep/sub/assistant.db", "dir/credentials", "a/b/c/config.py"]
        for p in blocked:
            with pytest.raises(SafePathError):
                resolve_safe_path(p, workspace_root=ws)


def test_file_manager_error_sanitization():
    """Verify that error messages do not disclose absolute host paths."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        err_res = read_file_content("non_existent_file_xyz.txt", workspace_root=ws)
        assert str(ws) not in err_res
        assert not re.search(r"[a-zA-Z]:[\\/]", err_res)


@pytest.mark.asyncio
async def test_model_capability_transient_failure_not_cached():
    """Verify that transient server errors (500, timeouts, rate limits) do NOT mark model tool-unsupported."""
    client = OllamaClient()
    model = "test-qwen-transient"
    assert client.is_tool_supported(model) is True

    # 1. Transient 500 error (e.g. internal CUDA error)
    mock_resp_500 = MagicMock()
    mock_resp_500.status_code = 500
    mock_resp_500.text = "Internal Server Error: CUDA out of memory"
    mock_resp_500.raise_for_status.side_effect = httpx.HTTPStatusError("500 Internal Server Error", request=MagicMock(), response=mock_resp_500)
    client.session.post = AsyncMock(return_value=mock_resp_500)

    with pytest.raises(httpx.HTTPStatusError):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model=model,
            tools=[{"type": "function", "function": {"name": "calc"}}],
            validate=False,
        )
    # MUST remain tool-supported (not permanently blacklisted!)
    assert client.is_tool_supported(model) is True

    # 2. Timeout exception
    client.session.post = AsyncMock(side_effect=httpx.TimeoutException("Read timed out"))
    with pytest.raises(httpx.TimeoutException):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model=model,
            tools=[{"type": "function", "function": {"name": "calc"}}],
            validate=False,
        )
    assert client.is_tool_supported(model) is True

    # 3. Connection error
    client.session.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(OllamaConnectionError):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model=model,
            tools=[{"type": "function", "function": {"name": "calc"}}],
            validate=False,
        )
    assert client.is_tool_supported(model) is True

    # 4. Rate limit 429
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.text = "Too Many Requests"
    mock_resp_429.raise_for_status.side_effect = httpx.HTTPStatusError("429 Too Many Requests", request=MagicMock(), response=mock_resp_429)
    client.session.post = AsyncMock(return_value=mock_resp_429)
    with pytest.raises(httpx.HTTPStatusError):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model=model,
            tools=[{"type": "function", "function": {"name": "calc"}}],
            validate=False,
        )
    assert client.is_tool_supported(model) is True


@pytest.mark.asyncio
async def test_model_capability_unsupported_tool_explicit_error_cached():
    """Verify that verified tool incompatibility errors mark model as unsupported and fallback to plain chat."""
    client = OllamaClient()
    model = "test-llama2-notools"
    assert client.is_tool_supported(model) is True

    # Ollama returns 400 with explicit rejection
    mock_err_resp = MagicMock()
    mock_err_resp.status_code = 400
    mock_err_resp.text = "error: model 'test-llama2-notools' does not support tools"

    # Fallback response without tools
    mock_ok_resp = MagicMock()
    mock_ok_resp.status_code = 200
    mock_ok_resp.json.return_value = {
        "message": {"role": "assistant", "content": "I am answering without tools."}
    }

    client.session.post = AsyncMock(side_effect=[mock_err_resp, mock_ok_resp])

    result = await client.chat(
        messages=[{"role": "user", "content": "What is 2+2?"}],
        model=model,
        tools=[{"type": "function", "function": {"name": "calc"}}],
        validate=False,
    )

    # Model must now be recorded as tool unsupported
    assert client.is_tool_supported(model) is False
    # Fallback chat answered cleanly without claiming tools ran
    assert result["content"] == "I am answering without tools."
    assert result["tool_calls"] == []
