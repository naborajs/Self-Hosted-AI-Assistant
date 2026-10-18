from __future__ import annotations

import logging

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from config import settings
from .models import Base


logger = logging.getLogger("local_ai_assistant.database")

engine: AsyncEngine | None = None
async_session: sessionmaker | None = None


async def init_db(settings) -> None:
    global engine, async_session
    if engine is None:
        db_url = str(settings.database_url)
        is_sqlite = "sqlite" in db_url.lower()

        engine = create_async_engine(settings.database_url, echo=False, future=True)

        if is_sqlite:
            @event.listens_for(engine.sync_engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                try:
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA foreign_keys = ON")
                    cursor.execute("PRAGMA busy_timeout = 5000")
                    cursor.close()
                except Exception as exc:
                    logger.debug("Failed to set SQLite connection pragmas: %s", exc)

        async_session = sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with engine.begin() as conn:
            if is_sqlite and ":memory:" not in db_url:
                try:
                    result = await conn.execute(text("PRAGMA journal_mode = WAL;"))
                    wal_mode = result.scalar()
                    logger.info("SQLite journal_mode set to: %s", wal_mode)
                except Exception as exc:
                    logger.warning("Could not set SQLite WAL mode: %s", exc)

            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database engine initialized and tables created")


async def shutdown_db() -> None:
    global engine, async_session
    if engine is not None:
        await engine.dispose()
        engine = None
        async_session = None
        logger.info("Database engine disposed")


def get_session() -> AsyncSession:
    if async_session is None:
        raise RuntimeError("Database session factory is not initialized")
    return async_session()

