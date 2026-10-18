"""User-scoped long-term memory store with deterministic lexical retrieval for Project EDITH v2.0.

This module provides the `MemoryStore` abstraction:
- User Isolation: All facts are strictly keyed and isolated by internal User ID.
- Storage Normalization: Enforces slug formatting, lowercase folding, and character limits
  via the repository layer.
- Deterministic Lexical Ranking:
  - Tokenizes queries into alphanumeric words, filtering out common conversational stop words.
  - Matches tokens against memory keys (weight 4 for exact, 2 for substring), tags (weight 2),
    and values (weight 1).
  - Implements deterministic multi-level tie-breaking: `score DESC`, `updated_at DESC`, `key ASC`.
- Context Window Protection: Caps retrieved memories to top 5 items and 600 total characters.
- Zero External Dependencies: Eliminates complex vector databases or embeddings models in favor
  of lightweight SQLite relational queries.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from config import settings
from database import repository


logger = logging.getLogger("local_ai_assistant.memory")

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "he", "in", "is", "it", "its", "of", "on", "that", "the",
    "to", "was", "were", "will", "with", "tell", "show", "what", "where",
    "when", "who", "how", "why", "can", "could", "would", "should", "me",
    "my", "you", "your", "i", "do", "does", "did", "please", "about",
}


@dataclass
class MemoryStore:
    """Manages user-scoped long-term memory records and keyword relevance ranking."""
    settings: Any

    async def initialize(self) -> None:
        self.settings.storage_path.mkdir(parents=True, exist_ok=True)
        self.settings.encrypted_sessions_path.mkdir(parents=True, exist_ok=True)
        logger.info("Memory storage initialized at %s", self.settings.storage_path)

    async def save_fact(self, user_id: int, key: str, value: str, tags: str | None = None) -> Any:
        """Create or update a user-scoped fact with normalization and validation."""
        record, _ = await repository.upsert_user_memory(user_id, key, value, tags)
        return record

    async def get_fact(self, user_id: int, key: str) -> Any:
        """Retrieve a user-scoped fact by its normalized key."""
        return await repository.get_user_memory_by_key(user_id, key)

    async def list_facts(self, user_id: int, limit: int = 50) -> list[Any]:
        """List all facts belonging to a user, ordered by recent update."""
        return await repository.list_user_memories(user_id, limit=limit)

    async def search_facts(self, user_id: int, query: str) -> list[Any]:
        """Query user facts containing the specified substring in key or tags."""
        if not query or not query.strip():
            return await repository.list_user_memories(user_id, limit=100)
        return await repository.query_memory_records(user_id, tags=query)

    async def update_fact(self, record_id: int, value: str, tags: str | None = None) -> Any:
        """Update an existing record by ID."""
        return await repository.update_memory_record(record_id, value, tags)

    async def delete_fact(self, record_id: int) -> None:
        """Delete a record by ID."""
        await repository.delete_memory_record(record_id)

    async def delete_fact_by_key(self, user_id: int, key: str) -> bool:
        """Delete a user-scoped record by its key."""
        return await repository.delete_user_memory_by_key(user_id, key)

    async def clear_all_facts(self, user_id: int) -> int:
        """Clear all records belonging to a user."""
        return await repository.delete_all_user_memories(user_id)

    async def retrieve_relevant_facts(
        self,
        user_id: int,
        query_text: str,
        max_count: int = 5,
        max_chars: int = 600,
    ) -> list[Any]:
        """Retrieve relevant memories for a user using deterministic keyword ranking.

        Limitations:
        - Keyword-based lexeme matching only. Does NOT use vector embeddings or semantic search.
        - Matches words against key (weight 3), tags (weight 2), and value (weight 1).
        - Deterministic tie-breaking: score DESC, updated_at DESC, key ASC.
        - Respects explicit size bounds (max_count, max_chars).
        """
        if not user_id or not query_text or not query_text.strip():
            return []

        # Tokenize query into lowercase alphanumeric words
        raw_tokens = re.findall(r"\b[a-zA-Z0-9_\-\.]{2,}\b", query_text.lower())
        tokens = [t for t in raw_tokens if t not in STOP_WORDS]
        if not tokens:
            return []

        all_memories = await repository.list_user_memories(user_id, limit=100)
        if not all_memories:
            return []

        scored_records: list[tuple[int, float, str, Any]] = []
        token_set = set(tokens)

        for record in all_memories:
            score = 0
            rec_key = (record.key or "").lower()
            rec_tags = (record.tags or "").lower()
            rec_val = (record.value or "").lower()

            key_words = set(re.findall(r"\b[a-zA-Z0-9_\-\.]{2,}\b", rec_key))
            tag_words = set(re.findall(r"\b[a-zA-Z0-9_\-\.]{2,}\b", rec_tags))
            val_words = set(re.findall(r"\b[a-zA-Z0-9_\-\.]{2,}\b", rec_val))

            # Direct key match
            for t in token_set:
                if t in key_words or t == rec_key:
                    score += 4
                elif any(t in kw for kw in key_words):
                    score += 2

                if t in tag_words:
                    score += 2

                if t in val_words:
                    score += 1

            if score > 0:
                ts = record.updated_at.timestamp() if getattr(record, "updated_at", None) else 0.0
                scored_records.append((score, ts, record.key, record))

        if not scored_records:
            return []

        # Sort deterministically: score DESC, timestamp DESC, key ASC
        scored_records.sort(key=lambda item: (-item[0], -item[1], item[2]))

        selected: list[Any] = []
        total_chars = 0
        seen_keys: set[str] = set()

        for _, _, key_name, record in scored_records:
            if key_name in seen_keys:
                continue
            entry_chars = len(key_name) + len(record.value or "") + 4
            if selected and (total_chars + entry_chars > max_chars):
                continue
            selected.append(record)


            seen_keys.add(key_name)
            total_chars += entry_chars
            if len(selected) >= max_count:
                break

        return selected

    async def summarize_conversation(self, conversation_id: int, summary: str) -> None:
        logger.debug("Summarize conversation %s: %s", conversation_id, summary)
        await asyncio.sleep(0)


def get_memory_store() -> MemoryStore:
    return MemoryStore(settings)
