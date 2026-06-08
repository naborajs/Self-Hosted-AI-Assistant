from __future__ import annotations

import httpx


def get_weather(location: str) -> str:
    url = "https://wttr.in/"
    params = {"format": "3", "q": location}
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
    return response.text.strip()
