"""User slash-command parsing and execution subsystem for Project EDITH v2.0.

This module intercepts and handles direct user control commands before messages
are passed to AI inference or tool execution pipelines:
- Model discovery and selection: `/models` and `/model <name>` (enforces admin privilege
  check if `allow_user_model_selection` is disabled).
- Presentation preferences: `/language <lang>` and `/style <concise|detailed|normal>`.
- Long-term memory operations: `/remember <key>: <value>`, `/memories`, `/forget <key>`,
  and `/forget all` (with 2-minute pending confirmation safety window).
- Controlled tool confirmations: `/confirm <token>` and `/cancel [token]` executing or
  aborting staged `PendingAction` records with brute-force attempt limits.
- System guidance: `/help` delivering contextual platform instructions.

All commands operate idempotently and return `(is_command: bool, reply_text: str | None)`.
"""

import json
import logging
import secrets
import time
from typing import Any

from ai.client import OllamaClient
from ai.tools import ToolRegistry
from database.models import User
from database.repository import (
    cancel_pending_action,
    consume_pending_action,
    get_active_pending_action,
    get_user_preference,
    record_failed_confirmation_attempt,
    resolve_effective_model,
    set_user_preference,
)

from memory.store import get_memory_store

logger = logging.getLogger("local_ai_assistant.ai_commands")

_pending_forget_all: dict[int, float] = {}
FORGET_ALL_CONFIRMATION_TIMEOUT_SECONDS = 120.0


def clear_pending_confirmations() -> None:
    """Clear all pending confirmation states (useful for tests and cleanup)."""
    _pending_forget_all.clear()

SUPPORTED_COMMANDS = {
    "/models", "/model", "/language", "/style",
    "/remember", "/memories", "/forget", "/help",
    "/confirm", "/cancel",
}



def is_user_authorized_for_model_switch(user: User, settings: Any) -> bool:
    """Determine whether an incoming user is authorized to switch models.
    
    Model switching is restricted to prevent unauthorized contacts from thrashing
    local compute resources with oversized models.
    """
    if getattr(user, "is_admin", False):
        return True

    admin_list = getattr(settings, "admin_list", [])
    if user.external_id in admin_list:
        return True

    clean_external = user.external_id.split(":", 1)[-1] if ":" in user.external_id else user.external_id
    if clean_external in admin_list:
        return True

    return bool(getattr(settings, "allow_user_model_selection", False))


async def handle_user_command(
    user: User,
    text: str | None,
    ai_client: OllamaClient,
    settings: Any,
    memory_store: Any | None = None,
    conversation_id: int | None = None,
    tool_registry: ToolRegistry | None = None,
) -> tuple[bool, str | None]:
    """Inspect an incoming message. If it is a personalization, model, memory, or tool confirmation command,
    process it idempotently and return (True, reply_text). Otherwise return (False, None).
    """

    if not text:
        return False, None

    stripped = text.strip()
    if not stripped.startswith("/"):
        return False, None

    parts = stripped.split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command not in SUPPORTED_COMMANDS:
        return False, None

    logger.info("Executing user command '%s' for user %s (%s)", command, user.id, user.external_id)

    # 1. /models - list installed models
    if command == "/models":
        is_available = await ai_client.is_ollama_available()
        if not is_available:
            return True, f"⚠️ Ollama service is unreachable at {ai_client.base_url}. Cannot list installed models."

        models = await ai_client.get_installed_models()
        if not models:
            return (
                True,
                "No models are currently installed in Ollama. Models must be installed on the host machine using 'ollama pull <model>'.",
            )

        lines = ["🤖 **Installed Ollama Models:**"]
        for m in models:
            details_parts = []
            if m.parameter_size:
                details_parts.append(f"Params: {m.parameter_size}")
            if m.quantization_level:
                details_parts.append(f"Quant: {m.quantization_level}")
            if m.size:
                size_mb = round(m.size / (1024 * 1024), 1)
                if size_mb >= 1024:
                    details_parts.append(f"Size: {round(size_mb / 1024, 2)} GB")
                else:
                    details_parts.append(f"Size: {size_mb} MB")

            detail_str = f" ({', '.join(details_parts)})" if details_parts else ""
            lines.append(f"• `{m.name}`{detail_str}")

        lines.append("\nUse `/model <name>` to select your active model.")
        return True, "\n".join(lines)

    # 2. /model - inspect or switch preferred model
    if command == "/model":
        if not arg:
            pref = await get_user_preference(user.id)
            installed_models = await ai_client.get_installed_models()
            model_names = [m.name for m in installed_models]
            effective, source, error = await resolve_effective_model(
                user.id,
                model_names,
                default_env_model=getattr(settings, "ollama_model", "qwen3:8b"),
            )
            preferred = pref.preferred_model if (pref and pref.preferred_model) else "None (using system default)"
            lines = [
                "📋 **Current Model Configuration:**",
                f"• **Your Preference:** `{preferred}`",
            ]
            if effective:
                lines.append(f"• **Active Effective Model:** `{effective}` (Source: {source.replace('_', ' ')})")
            else:
                lines.append(f"• **Active Effective Model:** None (⚠️ {error})")

            lines.append("\nUse `/models` to list installed models or `/model <name>` to select.")
            return True, "\n".join(lines)

        # Reset command
        if arg.lower() in ("reset", "default", "none"):
            await set_user_preference(user.id, clear_model=True)
            return True, "✅ Your model preference has been reset to the system default."

        # Model selection command - check authorization
        if not is_user_authorized_for_model_switch(user, settings):
            logger.warning("Unauthorized model switch attempt by user %s (%s)", user.id, user.external_id)
            return (
                True,
                "⛔ Permission Denied: Model switching is restricted to administrators. Contact the system owner to request access.",
            )

        # Validate that the model is actually installed
        is_installed = await ai_client.is_model_installed(arg)
        if not is_installed:
            return (
                True,
                f"❌ Model '{arg}' is not installed in Ollama. Use `/models` to view available models on this server.",
            )

        await set_user_preference(user.id, preferred_model=arg)
        return True, f"✅ Preferred model updated to `{arg}`. Your future requests will use this model."

    # 3. /language - inspect or update response language
    if command == "/language":
        if not arg:
            pref = await get_user_preference(user.id)
            current_lang = pref.preferred_language if (pref and pref.preferred_language) else "Default (English)"
            return (
                True,
                f"🌐 Current language preference: **{current_lang}**\nUse `/language <language>` to change (e.g. `/language Bengali`), or `/language reset` to clear.",
            )

        if arg.lower() in ("reset", "default", "none"):
            await set_user_preference(user.id, clear_language=True)
            return True, "✅ Language preference reset to default."

        lang_val = arg.strip()[:64]
        await set_user_preference(user.id, preferred_language=lang_val)
        return True, f"✅ Language preference set to **{lang_val}**."

    # 4. /style - inspect or update response style
    if command == "/style":
        valid_styles = {"concise", "detailed", "normal"}
        if not arg:
            pref = await get_user_preference(user.id)
            current_style = pref.response_style if (pref and pref.response_style) else "normal"
            return (
                True,
                f"🎨 Current response style: **{current_style}**\nSupported options: `concise`, `detailed`, `normal`\nUse `/style <style>` to change.",
            )

        style_val = arg.lower()
        if style_val in ("reset", "default"):
            await set_user_preference(user.id, clear_style=True)
            return True, "✅ Response style reset to normal."

        if style_val not in valid_styles:
            return True, f"❌ Invalid style '{arg}'. Supported options are: `concise`, `detailed`, or `normal`."

        await set_user_preference(user.id, response_style=style_val)
        return True, f"✅ Response style updated to **{style_val}**."

    mem = memory_store or get_memory_store()

    # 5. /remember - save or update a user fact
    if command == "/remember":
        if not arg:
            return (
                True,
                "ℹ️ Usage: `/remember <key>=<value>` (e.g. `/remember city=Seattle` or `/remember coffee=espresso`)",
            )

        if "=" in arg:
            key_part, val_part = arg.split("=", 1)
        elif ":" in arg:
            key_part, val_part = arg.split(":", 1)
        elif " " in arg:
            key_part, val_part = arg.split(maxsplit=1)
        else:
            return (
                True,
                "ℹ️ Usage: `/remember <key>=<value>` (e.g. `/remember city=Seattle` or `/remember coffee=espresso`)",
            )

        key_clean = key_part.strip()
        val_clean = val_part.strip()
        if not key_clean or not val_clean:
            return (
                True,
                "ℹ️ Both key and value must be provided. Usage: `/remember <key>=<value>`",
            )

        try:
            record = await mem.save_fact(user.id, key_clean, val_clean)
            return True, f"✅ Remembered: **{record.key}** = {record.value}"
        except ValueError as ve:
            return True, f"❌ Validation error: {str(ve)}"
        except Exception as exc:
            logger.error("Failed to save memory for user %s: %s", user.id, exc)
            return True, "⚠️ Failed to save memory due to an internal error. Please try again."

    # 6. /memories - list or search saved user facts
    if command == "/memories":
        if not arg:
            records = await mem.list_facts(user.id, limit=25)
            if not records:
                return (
                    True,
                    "🧠 You don't have any saved memories yet. Use `/remember <key>=<value>` to save one.",
                )
            lines = ["🧠 **Your Saved Memories:**"]
            for r in records:
                val_disp = r.value if len(r.value) <= 100 else (r.value[:97] + "...")
                lines.append(f"• **{r.key}**: {val_disp}")
            lines.append("\nUse `/forget <key>` to remove a memory or `/forget all` to clear all.")
            return True, "\n".join(lines)

        records = await mem.search_facts(user.id, arg)
        if not records:
            return True, f"🧠 No memories found matching '{arg}'."
        lines = [f"🧠 **Memories matching '{arg}':**"]
        for r in records:
            val_disp = r.value if len(r.value) <= 100 else (r.value[:97] + "...")
            lines.append(f"• **{r.key}**: {val_disp}")
        return True, "\n".join(lines)

    # 7. /forget - delete a specific fact or all facts
    if command == "/forget":
        if not arg:
            return (
                True,
                "ℹ️ Usage: `/forget <key>` to delete a specific memory, or `/forget all` to clear all memories.",
            )

        arg_lower = arg.lower()
        if arg_lower == "all":
            _pending_forget_all[user.id] = time.time() + FORGET_ALL_CONFIRMATION_TIMEOUT_SECONDS
            return (
                True,
                "⚠️ Are you sure you want to delete all your saved memories? Reply with `/forget all confirm` within 2 minutes to permanently clear your memory.",
            )

        if arg_lower in ("all confirm", "all_confirm"):
            expiry = _pending_forget_all.get(user.id)
            now = time.time()
            if not expiry or now > expiry:
                _pending_forget_all.pop(user.id, None)
                return (
                    True,
                    "⚠️ Confirmation expired or not requested. Please send `/forget all` first if you wish to clear all memories.",
                )
            _pending_forget_all.pop(user.id, None)
            count = await mem.clear_all_facts(user.id)
            return True, f"🗑️ All your saved memories have been cleared ({count} fact{'s' if count != 1 else ''} removed)."

        deleted = await mem.delete_fact_by_key(user.id, arg)
        clean_key = arg.strip().lower()
        if deleted:
            return True, f"🗑️ Forgotten: **{clean_key}**"
        return True, f"⚠️ No memory found for key: **{clean_key}**"

    # 8. /confirm - authorize and execute a pending tool action
    if command == "/confirm":
        action = await get_active_pending_action(user.id, conversation_id)
        if not action:
            return True, "⚠️ No pending action requires confirmation, or the confirmation window has expired."

        # If user submitted a token or affirmative keyword
        if arg:
            cleaned_arg = arg.strip().lower()
            expected_token = (action.confirmation_token or "").lower()
            if cleaned_arg in ("yes", "y", "confirm"):
                # Authorized via explicit affirmative confirmation
                authorized = True
            elif secrets.compare_digest(cleaned_arg, expected_token):
                # Authorized via exact matching token
                authorized = True
            else:
                authorized = False
                attempt_count, is_invalidated = await record_failed_confirmation_attempt(action.id)
                if is_invalidated:
                    return (
                        True,
                        "⛔ Confirmation failed: maximum attempt limit reached. The pending action has been cancelled.",
                    )
                remaining = max(0, action.max_attempts - attempt_count)
                return (
                    True,
                    f"⚠️ Invalid confirmation token ({remaining} attempt{'s' if remaining != 1 else ''} remaining). "
                    f"Reply with `/confirm <token>` or `/confirm yes` to authorize, or `/cancel` to decline.",
                )
        else:
            # User sent bare '/confirm' without argument - describe action and prompt for explicit confirmation
            return (
                True,
                f"⚠️ **Confirmation Required:**\n"
                f"EDITH is waiting for your authorization to execute:\n"
                f"• **Action**: {action.summary}\n"
                f"• **Tool**: `{action.tool_name}`\n\n"
                f"To authorize, reply with `/confirm yes` or `/confirm {action.confirmation_token}`.\n"
                f"To decline, reply with `/cancel`.\n"
                f"*(This authorization request expires in 2 minutes.)*",
            )

        consumed = await consume_pending_action(action.id, user_id=user.id, conversation_id=conversation_id)
        if not consumed:
            return True, "⚠️ Action has already been processed or expired."

        registry = tool_registry or ToolRegistry(settings)
        arguments = {}
        if action.arguments_json:
            try:
                arguments = json.loads(action.arguments_json)
            except Exception:
                arguments = {}

        logger.info(
            "Executing confirmed action id=%d tool='%s' for user=%d conv=%s",
            action.id,
            action.tool_name,
            user.id,
            conversation_id,
        )
        exec_result = await registry.execute_tool(action.tool_name, arguments)
        if exec_result.success:
            return (
                True,
                f"✅ **Action Confirmed and Executed:**\n{action.summary}\n\n**Result:**\n{exec_result.output}",
            )
        return (
            True,
            f"❌ **Action Confirmed but Execution Failed:**\n{exec_result.error or exec_result.output}",
        )

    # 9. /cancel - abort an active pending tool action
    if command == "/cancel":
        action = await get_active_pending_action(user.id, conversation_id)
        if not action:
            return True, "ℹ️ No pending action to cancel."

        cancelled = await cancel_pending_action(action.id, user_id=user.id, conversation_id=conversation_id)
        if cancelled:
            return True, f"🚫 **Action Cancelled:** Staged operation '{action.summary}' was aborted."
        return True, "⚠️ Unable to cancel action (it may have already been consumed or expired)."

    # 10. /help - command reference
    if command == "/help":
        help_text = (
            "🤖 **EDITH Assistant Commands:**\n\n"
            "**Model & Personalization:**\n"
            "• `/models` — List installed Ollama models\n"
            "• `/model [name]` — View or switch your active model\n"
            "• `/language [lang]` — Set response language (e.g. `/language Spanish`)\n"
            "• `/style [style]` — Set response style (`concise`, `detailed`, `normal`)\n\n"
            "**Long-Term Memory:**\n"
            "• `/remember <key>=<value>` — Save a personal fact (e.g. `/remember city=Seattle`)\n"
            "• `/memories [query]` — View your saved memories\n"
            "• `/forget <key>` — Delete a saved memory\n"
            "• `/forget all` — Clear all your saved memories\n\n"
            "**Tool Actions & Confirmations:**\n"
            "• `/confirm [token]` — Authorize and execute a staged action\n"
            "• `/cancel` — Decline and cancel a staged action"
        )
        return True, help_text


    return False, None
