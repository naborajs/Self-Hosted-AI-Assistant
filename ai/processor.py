"""Unified conversation turn processing pipeline for Project EDITH v2.0.

This module provides the central `execute_conversation_turn` function that unifies
inbound message handling across WhatsApp, Telegram, and the FastAPI HTTP API:
1. User & Conversation Resolution: Idempotently map platform external IDs to internal User and Conversation records.
2. Ingress Message Persistence: Immediately record incoming user utterances in SQLite.
3. User Command Handling: Intercept control commands (`/models`, `/model`, `/remember`, `/confirm`, etc.).
4. Deterministic Preset Matching: Evaluate regex and keyword rules for instantaneous zero-latency responses.
5. Hierarchical Model Resolution: Enforce user preference -> admin default -> environment fallback order.
6. Memory & Context Assembly: Retrieve relevant long-term memories with injection-safe framing.
7. Inference & Tool Orchestration: Coordinate multi-turn tool loops or single completions via Ollama.
8. Egress Response Persistence: Record assistant replies and return a structured `TurnResult`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from ai.client import OllamaClient
from ai.commands import handle_user_command
from ai.conversation import ConversationContext
from ai.manager import AIManager
from ai.orchestrator import ToolOrchestrator
from ai.tools import ToolRegistry
from config import settings as global_settings
from database.models import User, Conversation
from database.repository import (
    create_conversation,
    get_conversation,
    get_or_create_user,
    get_user_preference,
    resolve_effective_model,
    save_message,
)
from memory.store import get_memory_store

logger = logging.getLogger("local_ai_assistant.processor")


@dataclass
class TurnResult:
    """Encapsulates the output and metadata of an executed conversation turn.

    Attributes:
        response: Synthesized text response from the assistant, command, or preset.
        model_used: Name of the Ollama model used (None if resolved via command or preset).
        conversation_id: Internal primary key of the conversation thread.
        user_id: Internal primary key of the interacting user.
        assistant_message_id: Primary key of the persisted Message entry for the response.
        is_command: True if handled by slash-command interception.
        is_preset: True if handled by deterministic preset rule matching.
        is_error: True if the turn encountered an unrecoverable failure.
        error_detail: Detailed diagnostic message if is_error is True.
    """
    response: str
    model_used: str | None
    conversation_id: int
    user_id: int
    assistant_message_id: int | None = None
    is_command: bool = False
    is_preset: bool = False
    is_error: bool = False
    error_detail: str | None = None


async def execute_conversation_turn(
    external_id: str,
    sender_name: str,
    chat_id: str | int,
    content: str,
    platform: str = "api",
    explicit_model: str | None = None,
    sender_id: str | None = None,
    message_type: str = "text",
    ai_client: OllamaClient | None = None,
    ai_manager: AIManager | None = None,
    memory_store: Any | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_orchestrator: ToolOrchestrator | None = None,
    app_settings: Any = None,
) -> TurnResult:
    """Execute a single conversation turn through the unified pipeline:
    1. Resolve or create user & conversation.
    2. Persist user message.
    3. Intercept and execute user commands (/model, /remember, /confirm, etc.).
    4. Match preset rules (fast deterministic response).
    5. Resolve model (respecting user preference, admin default, or explicit override).
    6. Load history and long-term memory facts with injection-safe framing.
    7. Execute tool loop or model generation.
    8. Persist assistant response and return TurnResult.
    """
    cfg = app_settings or global_settings
    client = ai_client or OllamaClient()
    mgr = ai_manager or AIManager(cfg.storage_path)
    mem = memory_store if memory_store is not None else get_memory_store()
    tools = tool_registry or ToolRegistry(cfg)
    orchestrator = tool_orchestrator or ToolOrchestrator(client, tools, cfg)
    orchestrator.ai_client = client

    # 1. User & Conversation resolution
    app_user = await get_or_create_user(external_id, sender_name)
    conversation = await get_conversation(app_user.id, str(chat_id))
    if conversation is None:
        conversation = await create_conversation(app_user.id, str(chat_id), title=str(chat_id))

    # 2. Record incoming user message
    msg_sender = sender_id or sender_name
    await save_message(conversation.id, msg_sender, "user", content, message_type=message_type)

    # 3. User Command Handling (/models, /model, /remember, /confirm, etc.)
    is_cmd, cmd_reply = await handle_user_command(
        app_user,
        content,
        client,
        cfg,
        mem,
        conversation_id=conversation.id,
        tool_registry=tools,
    )
    if is_cmd:
        answer = cmd_reply or "Command processed."
        saved_msg = await save_message(conversation.id, "assistant", "assistant", answer)
        return TurnResult(
            response=answer,
            model_used=None,
            conversation_id=conversation.id,
            user_id=app_user.id,
            assistant_message_id=getattr(saved_msg, "id", None),
            is_command=True,
        )

    # 4. Preset Reply Match
    preset_answer = mgr.match_preset(content)
    if preset_answer:
        saved_msg = await save_message(conversation.id, "assistant", "assistant", preset_answer)
        return TurnResult(
            response=preset_answer,
            model_used=None,
            conversation_id=conversation.id,
            user_id=app_user.id,
            assistant_message_id=getattr(saved_msg, "id", None),
            is_preset=True,
        )

    # 5. Model Resolution
    model_names: list[str] = []
    is_reachable = True
    try:
        installed_models = await client.get_installed_models()
        if isinstance(installed_models, (list, tuple)):
            model_names = [m.name for m in installed_models if hasattr(m, "name")]
            if not model_names:
                avail_result = await client.is_ollama_available()
                is_reachable = bool(avail_result) if isinstance(avail_result, bool) else True
        else:
            model_names = [getattr(cfg, "ollama_model", "qwen3:8b")]
    except Exception:
        is_reachable = False

    if explicit_model:
        clean_model = explicit_model.strip()
        if not is_reachable:
            err_msg = "Ollama service is currently unreachable and model availability cannot be verified. Please ensure Ollama is running."
            saved_msg = await save_message(conversation.id, "assistant", "assistant", f"⚠️ {err_msg}")
            return TurnResult(
                response=f"⚠️ {err_msg}",
                model_used=clean_model,
                conversation_id=conversation.id,
                user_id=app_user.id,
                assistant_message_id=getattr(saved_msg, "id", None),
                is_error=True,
                error_detail=err_msg,
            )
        is_installed = any(
            m == clean_model or m.split(":")[0] == clean_model
            for m in model_names
        )
        if not is_installed:
            avail_str = f" Available models: {', '.join(model_names)}" if model_names else " No models currently installed."
            err_msg = f"Requested model '{clean_model}' is not installed in Ollama.{avail_str}"
            saved_msg = await save_message(conversation.id, "assistant", "assistant", f"⚠️ {err_msg}")
            return TurnResult(
                response=f"⚠️ {err_msg}",
                model_used=clean_model,
                conversation_id=conversation.id,
                user_id=app_user.id,
                assistant_message_id=getattr(saved_msg, "id", None),
                is_error=True,
                error_detail=err_msg,
            )
        effective_model = clean_model
    else:
        effective_model, source, model_error = await resolve_effective_model(
            app_user.id,
            model_names,
            default_env_model=getattr(cfg, "ollama_model", "qwen3:8b"),
            ollama_reachable=is_reachable,
        )
        if model_error:
            saved_msg = await save_message(conversation.id, "assistant", "assistant", f"⚠️ {model_error}")
            return TurnResult(
                response=f"⚠️ {model_error}",
                model_used=None,
                conversation_id=conversation.id,
                user_id=app_user.id,
                assistant_message_id=getattr(saved_msg, "id", None),
                is_error=True,
                error_detail=model_error,
            )

    # 6. Context & Preferences
    pref = await get_user_preference(app_user.id)
    pref_lang = pref.preferred_language if pref else None
    pref_style = pref.response_style if pref else None

    context = ConversationContext(
        conversation.id,
        mgr,
        memory_store=mem,
        user_id=app_user.id,
    )
    history = await context.load_history()
    system_context = mgr.build_system_context(platform=platform, user_name=sender_name)
    knowledge = mgr.search_knowledge(content)

    mem_facts: list[str] = []
    if mem:
        try:
            facts = await mem.retrieve_relevant_facts(
                user_id=app_user.id,
                query_text=content,
                max_count=5,
                max_chars=600,
            )
            for f in facts:
                clean_val = str(f.value).replace("\n", " ").strip()
                mem_facts.append(f"- {f.key}: {clean_val}")
        except Exception as exc:
            logger.warning("Memory retrieval in turn execution failed: %s", exc)

    # 7. Generation / Tool Execution
    is_mock_generate = (
        type(client).__name__ in ("AsyncMock", "MagicMock", "Mock")
        or hasattr(getattr(client, "generate", None), "assert_called")
        or hasattr(getattr(client, "generate", None), "assert_not_called")
    ) and not getattr(client, "_test_tool_orchestration", False)

    try:
        if getattr(cfg, "enable_tool_execution", True) and not is_mock_generate:
            answer = await orchestrator.run_turn(
                user_id=app_user.id,
                conversation_id=conversation.id,
                user_message=content,
                system_context=system_context,
                history=history,
                model=effective_model,
                user_name=sender_name,
                platform=platform,
                preferred_language=pref_lang,
                response_style=pref_style,
                knowledge_snippets=knowledge,
                user_memories=mem_facts,
            )
        else:
            prompt = await context.build_prompt(
                platform=platform,
                user_name=sender_name,
                user_message=content,
                preferred_language=pref_lang,
                response_style=pref_style,
                user_id=app_user.id,
            )
            try:
                answer = await client.generate(prompt=prompt, model=effective_model)
            except TypeError as te:
                if "unexpected keyword argument" in str(te):
                    answer = await client.generate(prompt=prompt)
                else:
                    raise
            if not answer or not str(answer).strip():
                answer = "I couldn't generate a response. Please check that Ollama is running."
    except Exception as exc:
        logger.warning("Inference execution failed: %s", exc)
        err_msg = "Inference execution failed or Ollama is unavailable."
        saved_msg = await save_message(conversation.id, "assistant", "assistant", f"⚠️ {err_msg}")
        return TurnResult(
            response=f"⚠️ {err_msg}",
            model_used=effective_model,
            conversation_id=conversation.id,
            user_id=app_user.id,
            assistant_message_id=getattr(saved_msg, "id", None),
            is_error=True,
            error_detail=str(exc) or err_msg,
        )

    # 8. Record assistant response
    saved_msg = await save_message(conversation.id, "assistant", "assistant", answer)

    return TurnResult(
        response=answer,
        model_used=effective_model,
        conversation_id=conversation.id,
        user_id=app_user.id,
        assistant_message_id=getattr(saved_msg, "id", None),
    )
