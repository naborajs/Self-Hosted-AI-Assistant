"""Telegram bot gateway subsystem for Project EDITH v2.0.

Provides direct integration with the Telegram Bot API:
- `bot.py`: Asynchronous long-polling bot gateway with non-fatal token handling,
  slash command routing, and unified 8-step turn pipeline execution.
"""

from .bot import TelegramGateway

__all__ = ["TelegramGateway"]
