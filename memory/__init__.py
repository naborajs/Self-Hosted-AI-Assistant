"""Persistent long-term memory storage subsystem for Project EDITH v2.0.

This package provides user-scoped factual memory persistence:
- Keyword-based lexeme ranking with deterministic scoring and tie-breaking.
- Bounded context window injection (maximum 600 characters, top 5 facts).
- Anti-injection untrusted reference framing.
- Idempotent upsert, deletion, and query operations backed by SQLite.
"""

from .store import MemoryStore, get_memory_store

__all__ = ["MemoryStore", "get_memory_store"]
