from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .connection import get_session
from .models import (
    Conversation,
    InboundMessage,
    MemoryRecord,
    Message,
    OutboundMessage,
    SystemSetting,
    User,
    UserPreference,
)


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


async def get_user_by_id(user_id: int) -> User | None:
    async with get_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
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
    conditions = [InboundMessage.status == "PROCESSING"]
    if stale_threshold_seconds <= 0:
        pass
    else:
        threshold_cutoff = now - timedelta(seconds=stale_threshold_seconds)
        conditions.append(
            or_(
                InboundMessage.lease_expires_at.is_(None),
                InboundMessage.lease_expires_at < now,
                InboundMessage.updated_at < threshold_cutoff,
            )
        )
    async with get_session() as session:
        result = await session.execute(
            select(InboundMessage).where(*conditions)
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


# ============================================================================
# Phase 2.1: Durable Outbound Delivery Repository Operations
# ============================================================================

async def create_or_get_outbound_message(
    chat_id: str,
    text: str | None = None,
    correlation_id: str | None = None,
    inbound_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
    platform: str = "whatsapp",
    file_url: str | None = None,
    mime_type: str | None = None,
    caption: str | None = None,
    max_attempts: int = 3,
) -> OutboundMessage:
    """Create a durable OutboundMessage or retrieve an existing one by correlation_id.

    Guarantees idempotency: even if multiple recovery workers execute Stage 2,
    only one OutboundMessage will ever be created per correlation_id.
    """
    if not correlation_id:
        if inbound_id:
            correlation_id = f"out_{platform}_{inbound_id}"
        else:
            correlation_id = f"out_{platform}_{int(datetime.utcnow().timestamp() * 1000)}"

    now = datetime.utcnow()
    async with get_session() as session:
        res = await session.execute(
            select(OutboundMessage).where(OutboundMessage.correlation_id == correlation_id)
        )
        existing = res.scalars().first()
        if existing:
            return existing

        outbound = OutboundMessage(
            inbound_id=inbound_id,
            conversation_id=conversation_id,
            message_id=message_id,
            correlation_id=correlation_id,
            platform=platform,
            chat_id=chat_id,
            text=text,
            file_url=file_url,
            mime_type=mime_type,
            caption=caption,
            status="PENDING",
            attempt_count=0,
            max_attempts=max_attempts,
            created_at=now,
            updated_at=now,
        )
        session.add(outbound)
        try:
            await session.commit()
            await session.refresh(outbound)
            return outbound
        except Exception:
            await session.rollback()
            res2 = await session.execute(
                select(OutboundMessage).where(OutboundMessage.correlation_id == correlation_id)
            )
            found = res2.scalars().first()
            if found:
                return found
            raise


async def claim_next_outbound_message(
    active_chat_ids: set[str],
    lease_seconds: float = 60.0,
    platform: str = "whatsapp",
) -> OutboundMessage | None:
    """Atomically lease the next pending outbound message for an inactive chat.

    Per-conversation serialization: guarantees that two outbound workers
    never dispatch messages for the same chat simultaneously.
    """
    now = datetime.utcnow()
    lease_expiry = now + timedelta(seconds=lease_seconds)

    async with get_session() as session:
        active_db_chats = select(OutboundMessage.chat_id).where(
            OutboundMessage.platform == platform,
            OutboundMessage.status == "SENDING",
            OutboundMessage.lease_expires_at.is_not(None),
            OutboundMessage.lease_expires_at >= now,
        )

        conditions = [
            OutboundMessage.platform == platform,
            OutboundMessage.chat_id.not_in(active_db_chats),
            or_(
                OutboundMessage.status == "PENDING",
                and_(
                    OutboundMessage.status == "SENDING",
                    or_(
                        OutboundMessage.lease_expires_at.is_(None),
                        OutboundMessage.lease_expires_at < now,
                    ),
                ),
            ),
        ]
        if active_chat_ids:
            conditions.append(OutboundMessage.chat_id.not_in(active_chat_ids))

        stmt = (
            select(OutboundMessage)
            .where(*conditions)
            .order_by(OutboundMessage.created_at.asc(), OutboundMessage.id.asc())
            .limit(1)
        )
        result = await session.execute(stmt)
        msg = result.scalars().first()

        if msg is None:
            return None

        msg.status = "SENDING"
        msg.attempt_count += 1
        msg.lease_expires_at = lease_expiry
        msg.updated_at = now
        await session.commit()
        await session.refresh(msg)
        return msg


async def mark_outbound_sending(
    outbound_id: int,
    lease_seconds: float = 60.0,
    attempts: int | None = None,
) -> None:
    """Mark an outbound message as currently SENDING with an active lease."""
    now = datetime.utcnow()
    lease_expiry = now + timedelta(seconds=lease_seconds)
    values = {
        "status": "SENDING",
        "lease_expires_at": lease_expiry,
        "updated_at": now,
    }
    if attempts is not None:
        values["attempt_count"] = attempts
    async with get_session() as session:
        await session.execute(
            update(OutboundMessage)
            .where(OutboundMessage.id == outbound_id)
            .values(**values)
        )
        await session.commit()


async def mark_outbound_sent(outbound_id: int) -> None:
    """Mark an outbound message as confirmed delivered by the external bridge."""
    now = datetime.utcnow()
    async with get_session() as session:
        await session.execute(
            update(OutboundMessage)
            .where(OutboundMessage.id == outbound_id)
            .values(
                status="SENT",
                sent_at=now,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        await session.commit()


async def mark_outbound_ambiguous(
    outbound_id: int,
    error_msg: str,
    attempts: int | None = None,
) -> None:
    """Mark an outbound message delivery outcome as ambiguous.

    Used when a network timeout occurs while awaiting the bridge's response.
    The message may or may not have been delivered externally. Blind retries are halted
    to prevent sending duplicate messages to the recipient.
    """
    now = datetime.utcnow()
    values = {
        "status": "AMBIGUOUS",
        "last_error": error_msg[:1000] if error_msg else None,
        "lease_expires_at": None,
        "updated_at": now,
    }
    if attempts is not None:
        values["attempt_count"] = attempts
    async with get_session() as session:
        await session.execute(
            update(OutboundMessage)
            .where(OutboundMessage.id == outbound_id)
            .values(**values)
        )
        await session.commit()


async def mark_outbound_failed(
    outbound_id: int,
    error_msg: str,
    attempts: int | None = None,
) -> None:
    """Mark an outbound message as permanently failed (dead-lettered)."""
    now = datetime.utcnow()
    values = {
        "status": "FAILED",
        "last_error": error_msg[:1000] if error_msg else None,
        "lease_expires_at": None,
        "updated_at": now,
    }
    if attempts is not None:
        values["attempt_count"] = attempts
    async with get_session() as session:
        await session.execute(
            update(OutboundMessage)
            .where(OutboundMessage.id == outbound_id)
            .values(**values)
        )
        await session.commit()


async def requeue_outbound_retry(
    outbound_id: int,
    error_msg: str,
    attempts: int | None = None,
) -> None:
    """Reset a transiently failed outbound message to PENDING for retry."""
    now = datetime.utcnow()
    async with get_session() as session:
        result = await session.execute(
            select(OutboundMessage).where(OutboundMessage.id == outbound_id)
        )
        msg = result.scalars().first()
        if not msg:
            return

        if attempts is not None:
            msg.attempt_count = attempts
        else:
            msg.attempt_count += 1

        msg.last_error = error_msg[:1000] if error_msg else None
        msg.updated_at = now
        if msg.attempt_count >= msg.max_attempts:
            msg.status = "FAILED"
            msg.lease_expires_at = None
        else:
            msg.status = "PENDING"
            msg.lease_expires_at = None
        await session.commit()


async def recover_stale_outbound_leases(stale_threshold_seconds: float = 60.0) -> int:
    """Recover outbound messages stuck in SENDING with an expired lease."""
    now = datetime.utcnow()
    conditions = [OutboundMessage.status == "SENDING"]
    if stale_threshold_seconds <= 0:
        pass
    else:
        threshold_cutoff = now - timedelta(seconds=stale_threshold_seconds)
        conditions.append(
            or_(
                OutboundMessage.lease_expires_at.is_(None),
                OutboundMessage.lease_expires_at < now,
                OutboundMessage.updated_at < threshold_cutoff,
            )
        )
    async with get_session() as session:
        result = await session.execute(
            select(OutboundMessage).where(*conditions)
        )
        stale_records = result.scalars().all()
        for rec in stale_records:
            if rec.attempt_count >= rec.max_attempts:
                rec.status = "FAILED"
            else:
                rec.status = "PENDING"
            rec.lease_expires_at = None
            rec.updated_at = now

        if stale_records:
            await session.commit()
        return len(stale_records)


async def get_outbound_by_inbound_id(inbound_id: int) -> OutboundMessage | None:
    async with get_session() as session:
        result = await session.execute(
            select(OutboundMessage).where(OutboundMessage.inbound_id == inbound_id)
        )
        return result.scalars().first()


async def get_outbound_message(outbound_id: int) -> OutboundMessage | None:
    async with get_session() as session:
        result = await session.execute(
            select(OutboundMessage).where(OutboundMessage.id == outbound_id)
        )
        return result.scalars().first()


async def count_pending_outbound(platform: str = "whatsapp") -> int:
    async with get_session() as session:
        result = await session.execute(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.platform == platform,
                OutboundMessage.status.in_(["PENDING", "SENDING"]),
            )
        )
        return result.scalar() or 0


# ============================================================================
# Phase 3: Adaptive Model Selection & User Preferences
# ============================================================================

async def get_user_preference(user_id: int) -> UserPreference | None:
    """Retrieve the preference record for a given user."""
    async with get_session() as session:
        result = await session.execute(
            select(UserPreference).where(UserPreference.user_id == user_id)
        )
        return result.scalars().first()


async def set_user_preference(
    user_id: int,
    preferred_model: str | None = None,
    preferred_language: str | None = None,
    response_style: str | None = None,
    clear_model: bool = False,
    clear_language: bool = False,
    clear_style: bool = False,
) -> UserPreference:
    """Create or update user personalization preferences with strict field isolation."""
    now = datetime.utcnow()
    async with get_session() as session:
        result = await session.execute(
            select(UserPreference).where(UserPreference.user_id == user_id)
        )
        pref = result.scalars().first()
        if not pref:
            pref = UserPreference(
                user_id=user_id,
                preferred_model=None if clear_model else (preferred_model.strip() if preferred_model else None),
                preferred_language=None if clear_language else (preferred_language.strip() if preferred_language else None),
                response_style=None if clear_style else (response_style.strip().lower() if response_style else None),
                created_at=now,
                updated_at=now,
            )
            session.add(pref)
        else:
            if clear_model:
                pref.preferred_model = None
            elif preferred_model is not None:
                pref.preferred_model = preferred_model.strip()

            if clear_language:
                pref.preferred_language = None
            elif preferred_language is not None:
                pref.preferred_language = preferred_language.strip()

            if clear_style:
                pref.response_style = None
            elif response_style is not None:
                pref.response_style = response_style.strip().lower()

            pref.updated_at = now

        try:
            await session.commit()
            await session.refresh(pref)
            return pref
        except Exception:
            await session.rollback()
            raise


async def get_system_setting(key: str, default: str | None = None) -> str | None:
    """Retrieve a global system configuration setting from SQLite."""
    async with get_session() as session:
        result = await session.execute(
            select(SystemSetting.value).where(SystemSetting.key == key)
        )
        val = result.scalar_one_or_none()
        return val if val is not None else default


async def set_system_setting(key: str, value: str) -> None:
    """Create or update a global system configuration setting in SQLite."""
    now = datetime.utcnow()
    async with get_session() as session:
        result = await session.execute(
            select(SystemSetting).where(SystemSetting.key == key)
        )
        setting = result.scalars().first()
        if setting:
            setting.value = value
            setting.updated_at = now
        else:
            setting = SystemSetting(key=key, value=value, updated_at=now)
            session.add(setting)
        await session.commit()


async def resolve_effective_model(
    user_id: int | None,
    installed_model_names: list[str] | set[str] | None,
    default_env_model: str | None = None,
    ollama_reachable: bool = True,
) -> tuple[str | None, str, str | None]:
    """Resolve the effective model for a user strictly adhering to precedence order:
    1. User's saved model preference (if set).
    2. Persisted admin default setting (if set).
    3. Configured environment setting (OLLAMA_MODEL).

    Crucial rules:
    - If the first applicable selection is missing, unavailable, or uninstalled,
      report the problem rather than silently choosing a lower-precedence model.
    - If Ollama is unreachable and no verified cached inventory exists, distinguish
      service unavailability from a confirmed missing model.
    - Do not represent an unverified model as confirmed installed.
    - Never automatically select an arbitrary model or download/substitute models.

    Returns: (effective_model, source, error_detail)
    """
    installed_set = set(installed_model_names) if installed_model_names else set()

    unreachable_diagnostic = (
        "Ollama service is currently unreachable and model availability cannot be verified. "
        "Please ensure Ollama is running."
    )

    # 1. User Preference
    if user_id is not None:
        pref = await get_user_preference(user_id)
        if pref and pref.preferred_model:
            selected = pref.preferred_model
            if selected in installed_set:
                return selected, "user_preference", None
            if not ollama_reachable and not installed_set:
                return None, "user_preference", unreachable_diagnostic
            return (
                None,
                "user_preference",
                f"Selected model '{selected}' is not installed in Ollama. Please choose an available model with /model or install it.",
            )

    # 2. Persisted Admin Default
    admin_default = await get_system_setting("global_default_model")
    if admin_default:
        if admin_default in installed_set:
            return admin_default, "admin_default", None
        if not ollama_reachable and not installed_set:
            return None, "admin_default", unreachable_diagnostic
        return (
            None,
            "admin_default",
            f"Configured admin default model '{admin_default}' is not installed in Ollama.",
        )

    # 3. Legacy Environment Default (OLLAMA_MODEL)
    if default_env_model:
        if default_env_model in installed_set:
            return default_env_model, "environment_default", None
        if not ollama_reachable and not installed_set:
            return None, "environment_default", unreachable_diagnostic
        return (
            None,
            "environment_default",
            f"Configured default model '{default_env_model}' is not installed in Ollama.",
        )

    return None, "none", "No model configured or selected. Please select an available model with /model <name>."
