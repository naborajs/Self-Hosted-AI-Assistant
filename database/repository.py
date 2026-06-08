from __future__ import annotations

from datetime import datetime

from sqlalchemy import insert, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from .connection import get_session
from .models import Conversation, Message, MemoryRecord, User


async def create_user(external_id: str, display_name: str, email: str | None = None, is_admin: bool = False) -> User:
    async with get_session() as session:
        user = User(
            external_id=external_id,
            display_name=display_name,
            email=email,
            is_admin=is_admin,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def get_user_by_external_id(external_id: str) -> User | None:
    async with get_session() as session:
        result = await session.execute(select(User).where(User.external_id == external_id))
        return result.scalars().first()


async def get_or_create_user(external_id: str, display_name: str, email: str | None = None) -> User:
    user = await get_user_by_external_id(external_id)
    if user is not None:
        return user
    return await create_user(external_id, display_name, email)


async def create_conversation(user_id: int, external_chat_id: str, title: str | None = None, conversation_metadata: str | None = None) -> Conversation:
    async with get_session() as session:
        conversation = Conversation(
            user_id=user_id,
            external_chat_id=external_chat_id,
            title=title,
            conversation_metadata=conversation_metadata,
            updated_at=datetime.utcnow(),
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return conversation


async def get_conversation(user_id: int, external_chat_id: str) -> Conversation | None:
    async with get_session() as session:
        result = await session.execute(
            select(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.external_chat_id == external_chat_id,
            )
        )
        return result.scalars().first()


async def save_message(conversation_id: int, sender: str, role: str, content: str, message_type: str = "text") -> Message:
    async with get_session() as session:
        message = Message(
            conversation_id=conversation_id,
            sender=sender,
            role=role,
            content=content,
            message_type=message_type,
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        return message


async def list_messages(conversation_id: int, limit: int = 100) -> list[Message]:
    async with get_session() as session:
        result = await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
            .limit(limit)
        )
        return result.scalars().all()


async def save_memory_record(user_id: int, key: str, value: str, tags: str | None = None) -> MemoryRecord:
    async with get_session() as session:
        record = MemoryRecord(
            user_id=user_id,
            key=key,
            value=value,
            tags=tags,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def query_memory_records(user_id: int, key: str | None = None, tags: str | None = None) -> list[MemoryRecord]:
    async with get_session() as session:
        query = select(MemoryRecord).where(MemoryRecord.user_id == user_id)
        if key:
            query = query.where(MemoryRecord.key == key)
        if tags:
            query = query.where(MemoryRecord.tags.contains(tags))
        result = await session.execute(query.order_by(MemoryRecord.updated_at.desc()))
        return result.scalars().all()


async def update_memory_record(record_id: int, value: str, tags: str | None = None) -> MemoryRecord | None:
    async with get_session() as session:
        await session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == record_id)
            .values(value=value, tags=tags, updated_at=datetime.utcnow())
        )
        await session.commit()
        result = await session.execute(select(MemoryRecord).where(MemoryRecord.id == record_id))
        return result.scalars().first()


async def delete_memory_record(record_id: int) -> None:
    async with get_session() as session:
        await session.execute(delete(MemoryRecord).where(MemoryRecord.id == record_id))
        await session.commit()
