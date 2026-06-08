from __future__ import annotations

import logging

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
        engine = create_async_engine(settings.database_url, echo=False, future=True)
        async_session = sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database engine initialized and tables created")


async def shutdown_db() -> None:
    global engine
    if engine is not None:
        await engine.dispose()
        logger.info("Database engine disposed")


def get_session() -> AsyncSession:
    if async_session is None:
        raise RuntimeError("Database session factory is not initialized")
    return async_session()
