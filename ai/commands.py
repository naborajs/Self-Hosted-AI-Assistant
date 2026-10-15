from __future__ import annotations

import logging
from typing import Any

from ai.client import OllamaClient
from database.models import User
from database.repository import (
    get_user_preference,
    resolve_effective_model,
    set_user_preference,
)

logger = logging.getLogger("local_ai_assistant.ai_commands")

SUPPORTED_COMMANDS = {"/models", "/model", "/language", "/style"}


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
) -> tuple[bool, str | None]:
    """Inspect an incoming message. If it is a personalization or model command,
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

    return False, None
