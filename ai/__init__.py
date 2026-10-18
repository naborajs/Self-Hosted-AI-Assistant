"""AI engine subsystem for Project EDITH v2.0.

This package coordinates all artificial intelligence and tool-execution capabilities:
- `client.py`: High-performance asynchronous HTTP client for local Ollama inference,
  supporting connection pooling, concurrency limits, and cached model discovery.
- `processor.py`: The single-entrypoint unified conversation turn pipeline serving
  WhatsApp, Telegram, and HTTP machine clients consistently.
- `orchestrator.py`: Multi-turn conversational tool execution engine with loop bounds
  and two-phase human confirmation staging for privileged operations.
- `commands.py`: Slash-command interceptor for model selection, memory auditing, and
  cryptographic confirmation/cancellation tokens.
- `conversation.py`: Context builder responsible for history windowing, prompt framing,
  and injection-safe long-term memory retrieval.
- `manager.py`: Identity, prompt template, preset rule matcher, and knowledge base manager.
- `tools.py`: Extensible tool registry converting Python callables to Ollama JSON schemas
  with permission categorization and argument validation.
"""

from .client import OllamaClient, OllamaError, OllamaConnectionError, ModelNotFoundError
from .commands import handle_user_command
from .conversation import ConversationContext
from .manager import AIManager
from .orchestrator import ToolOrchestrator
from .processor import execute_conversation_turn, TurnResult
from .tools import ToolRegistry, ToolDefinition, ToolExecutionResult, PermissionCategory

__all__ = [
    "OllamaClient",
    "OllamaError",
    "OllamaConnectionError",
    "ModelNotFoundError",
    "handle_user_command",
    "ConversationContext",
    "AIManager",
    "ToolOrchestrator",
    "execute_conversation_turn",
    "TurnResult",
    "ToolRegistry",
    "ToolDefinition",
    "ToolExecutionResult",
    "PermissionCategory",
]
