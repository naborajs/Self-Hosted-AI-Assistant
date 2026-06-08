from __future__ import annotations

import httpx


def search_web(query: str) -> str:
    with httpx.Client(timeout=10.0) as client:
        response = client.get(
            "https://api.duckduckgo.com/", params={"q": query, "format": "json", "no_redirect": 1}
        )
        response.raise_for_status()
        data = response.json()
    abstract = data.get("AbstractText") or data.get("Heading") or "No summary available."
    return f"Search result for '{query}': {abstract}"
