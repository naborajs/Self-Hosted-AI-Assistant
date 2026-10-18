from __future__ import annotations

import logging
from typing import Any
import httpx

from config import settings

logger = logging.getLogger("local_ai_assistant.tools.weather")


def get_weather(location: str, app_settings: Any = None) -> str:
    cfg = app_settings or settings
    if not getattr(cfg, "allow_external_network_tools", False):
        return "Error: External network tools are disabled by default for privacy. An administrator can enable web search and weather lookups by setting ALLOW_EXTERNAL_NETWORK_TOOLS=true in configuration."

    clean_loc = str(location or "").strip()
    if not clean_loc:
        return "Error: Location cannot be empty."
    if len(clean_loc) > 100:
        clean_loc = clean_loc[:100]

    timeout_sec = float(getattr(cfg, "tool_timeout_seconds", 10.0))
    url = "https://wttr.in/"
    params = {"format": "3", "q": clean_loc}
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
        text = response.text.strip()
        return text or f"Weather report unavailable for {clean_loc}."
    except httpx.TimeoutException:
        logger.warning("Weather request timed out for location: %s", clean_loc)
        return "Error: Weather request timed out."
    except httpx.HTTPStatusError as exc:
        logger.warning("Weather request HTTP error %s for location: %s", exc.response.status_code, clean_loc)
        return "Error: Weather service returned an error."
    except Exception as exc:
        logger.warning("Weather request failed: %s", exc)
        return "Error: Unable to fetch weather data at this time."
