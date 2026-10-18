"""Privacy-first web search tool for Project EDITH v2.0.

Queries DuckDuckGo's Instant Answer API without tracking:
- Gated by `allow_external_network_tools` setting (disabled by default for air-gap privacy).
- Query string sanitized and length-capped to 500 characters.
- Strict network timeout enforcement (`tool_timeout_seconds`).
"""

from __future__ import annotations

import logging
from typing import Any
import httpx

from config import settings

logger = logging.getLogger("local_ai_assistant.tools.search")


def search_web(query: str, app_settings: Any = None) -> str:
    """Execute a web search query via DuckDuckGo Instant Answer API."""
    cfg = app_settings or settings
    if not getattr(cfg, "allow_external_network_tools", False):
        return "Error: External network tools are disabled by default for privacy. An administrator can enable web search and weather lookups by setting ALLOW_EXTERNAL_NETWORK_TOOLS=true in configuration."

    clean_query = str(query or "").strip()
    if not clean_query:
        return "Error: Search query cannot be empty."
    if len(clean_query) > 500:
        clean_query = clean_query[:500]

    timeout_sec = float(getattr(cfg, "tool_timeout_seconds", 10.0))
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            response = client.get(
                "https://api.duckduckgo.com/",
                params={"q": clean_query, "format": "json", "no_redirect": 1},
            )
            response.raise_for_status()
            data = response.json()
        abstract = data.get("AbstractText") or data.get("Heading") or "No summary available."
        return f"Search result for '{clean_query}': {abstract}"
    except httpx.TimeoutException:
        logger.warning("Search request timed out for query: %s", clean_query[:50])
        return "Error: Search request timed out."
    except httpx.HTTPStatusError as exc:
        logger.warning("Search request HTTP error %s for query: %s", exc.response.status_code, clean_query[:50])
        return "Error: Search service returned an error."
    except Exception as exc:
        logger.warning("Search request failed: %s", exc)
        return "Error: Unable to fetch search results at this time."
