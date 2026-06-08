from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from config import settings
from database import repository


logger = logging.getLogger("local_ai_assistant.memory")


@dataclass
class MemoryStore:
    settings: Any

    async def initialize(self) -> None:
        self.settings.storage_path.mkdir(parents=True, exist_ok=True)
        self.settings.encrypted_sessions_path.mkdir(parents=True, exist_ok=True)
        logger.info("Memory storage initialized at %s", self.settings.storage_path)

    async def save_fact(self, user_id: int, key: str, value: str, tags: str | None = None) -> Any:
        return await repository.save_memory_record(user_id, key, value, tags)

    async def search_facts(self, user_id: int, query: str) -> list[Any]:
        return await repository.query_memory_records(user_id, tags=query)

    async def update_fact(self, record_id: int, value: str, tags: str | None = None) -> Any:
        return await repository.update_memory_record(record_id, value, tags)

    async def delete_fact(self, record_id: int) -> None:
        await repository.delete_memory_record(record_id)

    async def summarize_conversation(self, conversation_id: int, summary: str) -> None:
        logger.debug("Summarize conversation %s: %s", conversation_id, summary)
        await asyncio.sleep(0)


def get_memory_store() -> MemoryStore:
    return MemoryStore(settings)
