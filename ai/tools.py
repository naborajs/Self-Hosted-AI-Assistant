from __future__ import annotations

from types import ModuleType
from typing import Callable

from tools.calculator import calculate_expression
from tools.file_manager import list_files, read_file_content, write_file_content
from tools.search import search_web
from tools.weather import get_weather
from tools.youtube_assistant import summarize_youtube_video
from tools.minecraft_assistant import get_minecraft_help

ToolFunction = Callable[..., str]


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, ToolFunction] = {
            "calculator": calculate_expression,
            "search": search_web,
            "weather": get_weather,
            "file_manager:list": list_files,
            "file_manager:read": read_file_content,
            "file_manager:write": write_file_content,
            "youtube": summarize_youtube_video,
            "minecraft": get_minecraft_help,
        }

    def list_tools(self) -> list[str]:
        return list(self.tools.keys())

    def call(self, name: str, *args: str, **kwargs: str) -> str:
        if name not in self.tools:
            raise ValueError(f"Tool '{name}' is not registered")
        return self.tools[name](*args, **kwargs)
