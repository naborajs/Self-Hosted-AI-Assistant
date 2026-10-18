"""Extensible tool execution plugins for Project EDITH v2.0.

This package contains modular tool implementations that can be invoked by the AI:
- `calculator.py`: Safe arithmetic evaluator using AST node inspection.
- `file_manager.py`: Workspace-contained file manipulation with path traversal defense.
- `search.py`: Privacy-preserving web search via DuckDuckGo.
- `weather.py`: Minimalist weather reporting via wttr.in.
- `youtube_assistant.py`: YouTube video metadata parser and summary generator.
- `minecraft_assistant.py`: Domain-specific game guidance and crafting assistance.
"""

from .calculator import calculate_expression
from .file_manager import list_files, read_file_content, write_file_content
from .minecraft_assistant import get_minecraft_help
from .search import search_web
from .weather import get_weather
from .youtube_assistant import summarize_youtube_video

__all__ = [
    "calculate_expression",
    "list_files",
    "read_file_content",
    "write_file_content",
    "get_minecraft_help",
    "search_web",
    "get_weather",
    "summarize_youtube_video",
]
