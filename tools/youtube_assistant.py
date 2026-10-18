"""YouTube video metadata extraction and summary assistance tool for Project EDITH v2.0.

Parses standard YouTube URLs and shortlinks (`youtu.be`) to extract video IDs
and coordinate topic/timestamp summaries.
"""

from __future__ import annotations

import re
import httpx


def summarize_youtube_video(url: str) -> str:
    """Parse a YouTube URL, extract the video ID, and prepare a summary prompt."""
    if not url or not isinstance(url, str):
        return "Unable to parse YouTube video URL."
    video_id = _extract_video_id(url.strip())
    if not video_id:
        return "Unable to parse YouTube video URL."
    return f"YouTube Assistant: summary request received for video {video_id}. Please ask for specific timestamps or topics."



def _extract_video_id(url: str) -> str | None:
    match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if match:
        return match.group(1)
    return None
