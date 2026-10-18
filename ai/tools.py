"""Centralized tool registry, schema generation, and sandboxed execution for Project EDITH v2.0.

This module provides the core tool execution framework for LLM-directed actions:
- `PermissionCategory`: Categorizes tool privileges into `READ_ONLY`, `NETWORK`,
  and `CONFIRMATION_REQUIRED`.
- `ToolDefinition`: Encapsulates tool metadata, JSON Schema parameters, execution handlers,
  aliases, and confirmation flags.
- `ToolExecutionResult`: Captures standardized tool execution output, success flags,
  human-in-the-loop confirmation staging, and error diagnostics.
- `ToolRegistry`: Manages built-in tools (`calculator`, `file_manager_read`,
  `file_manager_write`, `web_search`, `weather`, `youtube_assistant`, `minecraft_assistant`),
  translates signatures to Ollama/OpenAI function schemas, validates JSON arguments,
  and executes handlers with timeout containment.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import inspect
import logging
from typing import Any, Callable

from config import settings
from tools.calculator import calculate_expression
from tools.file_manager import list_files, read_file_content, write_file_content
from tools.search import search_web
from tools.weather import get_weather
from tools.youtube_assistant import summarize_youtube_video
from tools.minecraft_assistant import get_minecraft_help

logger = logging.getLogger("local_ai_assistant.ai_tools")


class PermissionCategory(str, Enum):
    """Execution permission levels for registered AI tools."""
    READ_ONLY = "read_only"
    NETWORK = "network"
    CONFIRMATION_REQUIRED = "confirmation_required"


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    permission: PermissionCategory = PermissionCategory.READ_ONLY
    requires_confirmation: bool = False
    aliases: list[str] = field(default_factory=list)

    def to_ollama_format(self) -> dict[str, Any]:
        """Format as an Ollama / OpenAI function definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolExecutionResult:
    tool_name: str
    output: str
    success: bool
    requires_confirmation: bool = False
    pending_action_token: str | None = None
    error: str | None = None


class ToolRegistry:
    """Central registry and execution manager for AI tools."""

    def __init__(self, app_settings: Any = None) -> None:
        self.settings = app_settings or settings
        self._tools: dict[str, ToolDefinition] = {}
        self._alias_map: dict[str, str] = {}
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        self.register(
            ToolDefinition(
                name="calculator",
                description="Safely evaluate a mathematical expression (e.g. '2 + 2 * 10' or 'sqrt(144)').",
                parameters={
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "The mathematical expression to evaluate.",
                        }
                    },
                    "required": ["expression"],
                },
                handler=calculate_expression,
                permission=PermissionCategory.READ_ONLY,
                requires_confirmation=False,
            )
        )

        self.register(
            ToolDefinition(
                name="search",
                description="Search the web for current or factual information using DuckDuckGo.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search keywords or query.",
                        }
                    },
                    "required": ["query"],
                },
                handler=self._wrap_with_settings(search_web),
                permission=PermissionCategory.NETWORK,
                requires_confirmation=False,
            )
        )

        self.register(
            ToolDefinition(
                name="weather",
                description="Get current weather information and forecast for a given city or location.",
                parameters={
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "City name or location (e.g. 'London', 'Tokyo').",
                        }
                    },
                    "required": ["location"],
                },
                handler=self._wrap_with_settings(get_weather),
                permission=PermissionCategory.NETWORK,
                requires_confirmation=False,
            )
        )

        self.register(
            ToolDefinition(
                name="file_manager_list",
                description="List files and directories within the safe, isolated storage workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Relative directory path (defaults to workspace root '.').",
                        }
                    },
                    "required": [],
                },
                handler=self._wrap_with_settings(list_files),
                permission=PermissionCategory.READ_ONLY,
                requires_confirmation=False,
                aliases=["file_manager:list"],
            )
        )

        self.register(
            ToolDefinition(
                name="file_manager_read",
                description="Read text contents from a file within the safe, isolated storage workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path of the file to read.",
                        }
                    },
                    "required": ["file_path"],
                },
                handler=self._wrap_with_settings(read_file_content),
                permission=PermissionCategory.READ_ONLY,
                requires_confirmation=False,
                aliases=["file_manager:read"],
            )
        )

        self.register(
            ToolDefinition(
                name="file_manager_write",
                description="Write text contents to a file within the safe storage workspace. Requires explicit user confirmation.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path of the file to write.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Text content to write into the file.",
                        },
                    },
                    "required": ["file_path", "content"],
                },
                handler=self._wrap_with_settings(write_file_content),
                permission=PermissionCategory.CONFIRMATION_REQUIRED,
                requires_confirmation=True,
                aliases=["file_manager:write"],
            )
        )

        self.register(
            ToolDefinition(
                name="youtube",
                description="Parse and summarize a YouTube video URL.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The YouTube video URL.",
                        }
                    },
                    "required": ["url"],
                },
                handler=summarize_youtube_video,
                permission=PermissionCategory.READ_ONLY,
                requires_confirmation=False,
            )
        )

        self.register(
            ToolDefinition(
                name="minecraft",
                description="Look up gameplay, crafting, redstone, or farming advice for Minecraft.",
                parameters={
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Minecraft topic (e.g. 'redstone', 'farm', 'nether').",
                        }
                    },
                    "required": ["topic"],
                },
                handler=get_minecraft_help,
                permission=PermissionCategory.READ_ONLY,
                requires_confirmation=False,
            )
        )

    def _wrap_with_settings(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap functions that accept app_settings/settings so they receive current settings."""
        sig = inspect.signature(func)
        param_names = set(sig.parameters.keys())

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if "app_settings" in param_names and "app_settings" not in kwargs:
                kwargs["app_settings"] = self.settings
            elif "settings" in param_names and "settings" not in kwargs:
                kwargs["settings"] = self.settings
            return func(*args, **kwargs)

        return wrapper

    def register(self, definition: ToolDefinition) -> None:
        """Register a tool definition and its aliases."""
        self._tools[definition.name] = definition
        for alias in definition.aliases:
            self._alias_map[alias] = definition.name

    def get_tool(self, name: str) -> ToolDefinition | None:
        """Retrieve tool definition by primary name or alias."""
        clean = (name or "").strip()
        canonical = self._alias_map.get(clean, clean)
        return self._tools.get(canonical)

    def requires_confirmation(self, name: str) -> bool:
        tool = self.get_tool(name)
        return bool(tool.requires_confirmation) if tool else False

    def list_tools(self) -> list[str]:
        """Return list of registered tool names including legacy aliases for backward compatibility."""
        res: list[str] = []
        for name, tool in self._tools.items():
            if tool.aliases:
                for alias in tool.aliases:
                    if alias not in res:
                        res.append(alias)
            elif name not in res:
                res.append(name)
        return res

    def get_tool_definitions(self) -> list[ToolDefinition]:
        """Return list of registered ToolDefinition objects."""
        return list(self._tools.values())


    @property
    def tools(self) -> dict[str, Callable[..., Any]]:
        """Legacy dictionary access for backward compatibility."""
        out: dict[str, Callable[..., Any]] = {}
        for name, tool in self._tools.items():
            out[name] = tool.handler
            for alias in tool.aliases:
                out[alias] = tool.handler
        return out

    def get_ollama_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions formatted for Ollama /api/chat 'tools' field."""
        return [tool.to_ollama_format() for tool in self._tools.values()]

    def validate_arguments(self, tool: ToolDefinition, arguments: dict[str, Any]) -> tuple[bool, str | None]:
        """Validate input arguments against tool schema parameters."""
        props = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])

        if not isinstance(arguments, dict):
            return False, f"Arguments for tool '{tool.name}' must be a JSON dictionary."

        for req_field in required:
            if req_field not in arguments or arguments[req_field] is None:
                return False, f"Missing required parameter '{req_field}' for tool '{tool.name}'."

        for key, val in arguments.items():
            if key in props:
                expected_type = props[key].get("type")
                if expected_type == "string" and not isinstance(val, str):
                    return False, f"Parameter '{key}' must be a string, got {type(val).__name__}."
                if expected_type == "integer" and not isinstance(val, int):
                    return False, f"Parameter '{key}' must be an integer, got {type(val).__name__}."
                if expected_type == "number" and not isinstance(val, (int, float)):
                    return False, f"Parameter '{key}' must be a number, got {type(val).__name__}."
                if expected_type == "boolean" and not isinstance(val, bool):
                    return False, f"Parameter '{key}' must be a boolean, got {type(val).__name__}."

        return True, None

    def call(self, name: str, *args: Any, **kwargs: Any) -> str:
        """Synchronously invoke a tool (backward compatibility)."""
        tool = self.get_tool(name)
        if tool is None:
            raise ValueError(f"Tool '{name}' is not registered")
        try:
            res = tool.handler(*args, **kwargs)
            return str(res)
        except Exception as exc:
            logger.warning("Tool execution error in '%s': %s", name, exc)
            raise

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
        max_output_chars: int | None = None,
    ) -> ToolExecutionResult:
        """Safely and asynchronously invoke a tool with validation, timeouts, and bounds."""
        clean_name = (name or "").strip()
        tool = self.get_tool(clean_name)
        if tool is None:
            err = f"Error: Tool '{clean_name}' is not recognized or available."
            return ToolExecutionResult(tool_name=clean_name, output=err, success=False, error=err)

        args = arguments if arguments is not None else {}
        is_valid, validation_err = self.validate_arguments(tool, args)
        if not is_valid:
            err = f"Error: {validation_err}"
            return ToolExecutionResult(tool_name=tool.name, output=err, success=False, error=err)

        # Network policy check
        if tool.permission == PermissionCategory.NETWORK:
            if not getattr(self.settings, "allow_external_network_tools", False):
                err = "Error: External network tools are disabled by default for privacy. An administrator can enable web search and weather lookups by setting ALLOW_EXTERNAL_NETWORK_TOOLS=true in configuration."
                return ToolExecutionResult(tool_name=tool.name, output=err, success=False, error=err)

        timeout_sec = timeout if timeout is not None else float(getattr(self.settings, "tool_timeout_seconds", 15.0))
        output_limit = max_output_chars if max_output_chars is not None else int(getattr(self.settings, "tool_max_output_chars", 2000))

        try:
            if inspect.iscoroutinefunction(tool.handler):
                coro = tool.handler(**args)
                raw_result = await asyncio.wait_for(coro, timeout=timeout_sec)
            else:
                loop = asyncio.get_running_loop()
                raw_result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: tool.handler(**args)),
                    timeout=timeout_sec,
                )

            output_str = str(raw_result) if raw_result is not None else ""
            if len(output_str) > output_limit:
                output_str = output_str[:output_limit] + "\n[Output truncated due to length limits]"

            return ToolExecutionResult(tool_name=tool.name, output=output_str, success=True)

        except asyncio.TimeoutError:
            err = f"Error: Tool '{tool.name}' timed out after {timeout_sec:.1f} seconds."
            logger.warning(err)
            return ToolExecutionResult(tool_name=tool.name, output=err, success=False, error=err)
        except Exception as exc:
            err = f"Error executing '{tool.name}': {type(exc).__name__}: {str(exc)[:150]}"
            logger.warning(err)
            return ToolExecutionResult(tool_name=tool.name, output=err, success=False, error=err)
