from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import datetime, timedelta
import pytest
from sqlalchemy import text

from database.connection import init_db, shutdown_db, get_session
from database.models import User, Conversation, Message
from database.repository import list_messages, create_user, create_conversation


class MockSettings:
    def __init__(self, db_url: str):
        self.database_url = db_url


@contextlib.asynccontextmanager
async def create_temp_file_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite+aiosqlite:///{path}"
    settings = MockSettings(db_url)
    await init_db(settings)
    try:
        yield {"path": path, "db_url": db_url, "settings": settings}
    finally:
        await shutdown_db()
        for ext in ["", "-wal", "-shm"]:
            fpath = path + ext
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass


@pytest.mark.asyncio
async def test_sqlite_wal_and_pragmas():
    # Verify WAL mode, foreign keys, and busy timeout on file-backed SQLite database
    async with create_temp_file_db():
        async with get_session() as session:
            wal_res = await session.execute(text("PRAGMA journal_mode;"))
            assert wal_res.scalar().lower() == "wal"

            fk_res = await session.execute(text("PRAGMA foreign_keys;"))
            assert fk_res.scalar() == 1

            timeout_res = await session.execute(text("PRAGMA busy_timeout;"))
            assert timeout_res.scalar() == 5000


@pytest.mark.asyncio
async def test_list_messages_window_and_chronological_order():
    async with create_temp_file_db():
        user = await create_user("test:user1", "Test User")
        conv1 = await create_conversation(user.id, "chat-1", "Chat 1")
        conv2 = await create_conversation(user.id, "chat-2", "Chat 2")

        base_time = datetime(2026, 1, 1, 10, 0, 0)

        # Insert 30 ordered messages into conv1
        async with get_session() as session:
            for i in range(1, 31):
                msg = Message(
                    conversation_id=conv1.id,
                    sender="user" if i % 2 != 0 else "assistant",
                    role="user" if i % 2 != 0 else "assistant",
                    content=f"Message {i}",
                    created_at=base_time + timedelta(minutes=i),
                )
                session.add(msg)

            # Insert 5 messages into conv2 to verify conversation isolation
            for j in range(1, 6):
                msg2 = Message(
                    conversation_id=conv2.id,
                    sender="user",
                    role="user",
                    content=f"Other Chat Message {j}",
                    created_at=base_time + timedelta(minutes=j),
                )
                session.add(msg2)
            await session.commit()

        # Criteria 1 & 2: For 30 ordered messages and limit of 12, returns messages 19 through 30 in chronological order
        recent_12 = await list_messages(conv1.id, limit=12)
        assert len(recent_12) == 12
        expected_contents = [f"Message {i}" for i in range(19, 31)]
        actual_contents = [m.content for m in recent_12]
        assert actual_contents == expected_contents

        # Verify chronological ascending order (oldest to newest within the window)
        for i in range(len(recent_12) - 1):
            assert recent_12[i].created_at <= recent_12[i + 1].created_at

        # Criteria 3: Smaller histories work correctly
        recent_5 = await list_messages(conv2.id, limit=12)
        assert len(recent_5) == 5
        assert [m.content for m in recent_5] == [f"Other Chat Message {j}" for j in range(1, 6)]

        # Criteria 4: Empty conversation returns empty list
        empty_conv = await create_conversation(user.id, "chat-empty", "Empty")
        empty_msgs = await list_messages(empty_conv.id, limit=12)
        assert empty_msgs == []


@pytest.mark.asyncio
async def test_list_messages_deterministic_tie_breaking():
    async with create_temp_file_db():
        user = await create_user("test:user2", "Tie User")
        conv = await create_conversation(user.id, "chat-ties", "Tie Chat")

        identical_timestamp = datetime(2026, 1, 1, 12, 0, 0)

        # Insert 10 messages with identical timestamp
        async with get_session() as session:
            for i in range(1, 11):
                msg = Message(
                    conversation_id=conv.id,
                    sender="user",
                    role="user",
                    content=f"Tie Message {i}",
                    created_at=identical_timestamp,
                )
                session.add(msg)
            await session.commit()

        # Query with limit 5: must deterministically return the last 5 created (messages 6..10) in ascending order by ID
        recent_5 = await list_messages(conv.id, limit=5)
        assert len(recent_5) == 5
        assert [m.content for m in recent_5] == [f"Tie Message {i}" for i in range(6, 11)]
        assert [m.id for m in recent_5] == sorted([m.id for m in recent_5])
