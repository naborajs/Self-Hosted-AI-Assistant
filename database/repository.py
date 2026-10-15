from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .connection import get_session
from .models import Conversation, InboundMessage, MemoryRecord, Message, User


async def create_user(external_id: str, display_name: str, email: str | None = None, is_admin: bool = False) -> User:
    async with get_session() as session:
        user = User(
            external_id=external_id,
            display_name=display_name,
            email=email,
            is_admin=is_admin,
        )
        session.add(user)
        try:
            await session.commit()
            await session.refresh(user)
            return user
        except Exception:
            await session.rollback()
            existing = await session.execute(select(User).where(User.external_id == external_id))
            user_found = existing.scalars().first()
            if user_found is not None:
                return user_found
            raise


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
        try:
            await session.commit()
            await session.refresh(conversation)
            return conversation
        except Exception:
            await session.rollback()
            existing = await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.external_chat_id == external_chat_id,
                )
            )
            conv_found = existing.scalars().first()
            if conv_found is not None:
                return conv_found
            raise


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
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        messages = list(result.scalars().all())
        messages.reverse()
        return messages


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


# ============================================================================
# Phase 2: Durable Inbound Inbox Repository Operations
# ============================================================================

async def save_inbound_batch(items: list[dict], platform: str = "whatsapp") -> list[str]:
    """Persist a batch of inbound messages atomically.

    Idempotent: If an (platform, external_id) already exists, it is skipped
    without error and its ID is returned in the acknowledged set.
    """
    if not items:
        return []

    persisted_external_ids: list[str] = []
    async with get_session() as session:
        ext_ids = [item.get("message_id") for item in items if item.get("message_id")]
        existing_set = set()
        if ext_ids:
            existing_result = await session.execute(
                select(InboundMessage.external_id).where(
                    InboundMessage.platform == platform,
                    InboundMessage.external_id.in_(ext_ids),
                )
            )
            existing_set = set(existing_result.scalars().all())

        for item in items:
            ext_id = item.get("message_id")
            if not ext_id:
                continue

            if ext_id in existing_set:
                persisted_external_ids.append(ext_id)
                continue

            inbound_msg = InboundMessage(
                platform=platform,
                external_id=ext_id,
                chat_id=item.get("chat_id") or "",
                sender=item.get("sender") or item.get("sender_id") or "",
                sender_name=item.get("sender_name"),
                content=item.get("content") or "",
                message_type=item.get("message_type") or "text",
                file_url=item.get("media_url") or item.get("file_url"),
                mime_type=item.get("mime_type"),
                caption=item.get("caption"),
                status="RECEIVED",
                attempt_count=0,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(inbound_msg)
            persisted_external_ids.append(ext_id)
            existing_set.add(ext_id)

        await session.commit()
    return persisted_external_ids


async def claim_next_inbound_message(
    active_chat_ids: set[str],
    lease_seconds: float = 120.0,
    max_attempts: int = 3,
    platform: str = "whatsapp",
) -> InboundMessage | None:
    """Atomically lease the next pending message for an inactive chat.

    Guarantees per-conversation FIFO ordering and prevents multiple workers
    from concurrently processing messages for the same chat.
    """
    now = datetime.utcnow()
    lease_expiry = now + timedelta(seconds=lease_seconds)

    async with get_session() as session:
        active_db_chats = select(InboundMessage.chat_id).where(
            InboundMessage.platform == platform,
            InboundMessage.status == "PROCESSING",
            InboundMessage.lease_expires_at.is_not(None),
            InboundMessage.lease_expires_at >= now,
        )

        conditions = [
            InboundMessage.platform == platform,
            InboundMessage.attempt_count < max_attempts,
            InboundMessage.chat_id.not_in(active_db_chats),
            or_(
                InboundMessage.status.in_(["RECEIVED", "RESPONSE_READY"]),
                and_(
                    InboundMessage.status == "PROCESSING",
                    or_(
                        InboundMessage.lease_expires_at.is_(None),
                        InboundMessage.lease_expires_at < now,
                    ),
                ),
            ),
        ]
        if active_chat_ids:
            conditions.append(InboundMessage.chat_id.not_in(active_chat_ids))

        stmt = (
            select(InboundMessage)
            .where(*conditions)
            .order_by(InboundMessage.created_at.asc(), InboundMessage.id.asc())
            .limit(1)
        )
        result = await session.execute(stmt)
        msg = result.scalars().first()

        if msg is None:
            return None

        if msg.status != "RESPONSE_READY":
            msg.status = "PROCESSING"
        msg.attempt_count += 1
        msg.lease_expires_at = lease_expiry
        msg.updated_at = now
        await session.commit()
        await session.refresh(msg)
        return msg


async def mark_inbound_response_ready(inbound_id: int, response_text: str) -> None:
    now = datetime.utcnow()
    async with get_session() as session:
        await session.execute(
            update(InboundMessage)
            .where(InboundMessage.id == inbound_id)
            .values(
                response_text=response_text,
                status="RESPONSE_READY",
                updated_at=now,
            )
        )
        await session.commit()


async def mark_inbound_completed(inbound_id: int) -> None:
    now = datetime.utcnow()
    async with get_session() as session:
        await session.execute(
            update(InboundMessage)
            .where(InboundMessage.id == inbound_id)
            .values(
                status="COMPLETED",
                lease_expires_at=None,
                updated_at=now,
            )
        )
        await session.commit()


async def mark_inbound_failed(
    inbound_id: int,
    error_msg: str,
    permanent: bool = False,
    max_attempts: int = 3,
) -> None:
    now = datetime.utcnow()
    async with get_session() as session:
        result = await session.execute(
            select(InboundMessage).where(InboundMessage.id == inbound_id)
        )
        msg = result.scalars().first()
        if not msg:
            return

        msg.last_error = error_msg[:1000] if error_msg else None
        msg.updated_at = now
        if permanent or msg.attempt_count >= max_attempts:
            msg.status = "FAILED"
            msg.lease_expires_at = None
        else:
            msg.status = "RECEIVED"
            msg.lease_expires_at = None
        await session.commit()


async def recover_stale_inbound_leases(stale_threshold_seconds: float = 120.0) -> int:
    now = datetime.utcnow()
    async with get_session() as session:
        result = await session.execute(
            select(InboundMessage).where(
                InboundMessage.status == "PROCESSING",
                or_(
                    InboundMessage.lease_expires_at.is_(None),
                    InboundMessage.lease_expires_at < now,
                ),
            )
        )
        stale_records = result.scalars().all()
        for rec in stale_records:
            if rec.response_text:
                rec.status = "RESPONSE_READY"
            else:
                rec.status = "RECEIVED"
            rec.lease_expires_at = None
            rec.updated_at = now

        if stale_records:
            await session.commit()
        return len(stale_records)


async def count_pending_inbound(platform: str = "whatsapp") -> int:
    async with get_session() as session:
        result = await session.execute(
            select(func.count(InboundMessage.id)).where(
                InboundMessage.platform == platform,
                InboundMessage.status.in_(["RECEIVED", "PROCESSING", "RESPONSE_READY"]),
            )
        )
        return result.scalar() or 0


async def get_inbound_message(inbound_id: int) -> InboundMessage | None:
    async with get_session() as session:
        result = await session.execute(
            select(InboundMessage).where(InboundMessage.id == inbound_id)
        )
        return result.scalars().first()
