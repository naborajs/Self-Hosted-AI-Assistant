from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from database.repository import list_messages, save_message


@dataclass
class ConversationContext:
    conversation_id: int
    ai_manager: Any
    memory_store: Any | None = None
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

    async def build_prompt(self, platform: str, user_name: str, user_message: str) -> str:
        system_context = self.ai_manager.build_system_context(platform=platform, user_name=user_name)
        knowledge = self.ai_manager.search_knowledge(user_message)
        history = await self.load_history()

        lines = [f"System: {system_context}"]
        if knowledge:
            lines.append("Knowledge snippets:")
            for snippet in knowledge:
                lines.append(f"- {snippet.get('title')}: {snippet.get('content')}")

        if history:
            lines.append("Conversation history:")
            for record in history:
                lines.append(f"{record['role'].capitalize()}: {record['content']}")

        lines.append(f"User: {user_message}")
        lines.append("Assistant:")
        return "\n".join(lines)
