from __future__ import annotations

from dataclasses import dataclass
from typing import List

from database.repository import list_messages, save_message


@dataclass
class ConversationContext:
    conversation_id: int
    system_prompt: str = "You are a secure local AI assistant assisting via WhatsApp and Telegram."
    max_history: int = 12

    async def load_history(self) -> list[dict[str, str]]:
        messages = await list_messages(self.conversation_id, limit=self.max_history)
        history: list[dict[str, str]] = []
        for message in messages:
            history.append({"role": message.role, "content": message.content})
        return history

    async def append_user_message(self, sender: str, content: str) -> None:
        await save_message(self.conversation_id, sender, "user", content)

    async def append_assistant_message(self, content: str) -> None:
        await save_message(self.conversation_id, "assistant", "assistant", content)

    async def build_prompt(self) -> str:
        history = await self.load_history()
        lines = [f"System: {self.system_prompt}"]
        for record in history:
            role = record["role"]
            content = record["content"]
            lines.append(f"{role.capitalize()}: {content}")
        return "\n".join(lines)
