"""SQLAlchemy declarative data models for Project EDITH v2.0.

This module defines the relational schema for EDITH's single-node persistence layer:
- Multi-channel identity mapping (`User`, `UserPreference`, `SystemSetting`)
- Dialogue state and history tracking (`Conversation`, `Message`)
- Long-term memory records (`MemoryRecord`)
- Asynchronous durable message queues (`InboundMessage`, `OutboundMessage`)
- Two-phase authorization and action safety (`PendingAction`)

All models inherit from a common DeclarativeBase configured for SQLite WAL mode
with foreign key cascading and deterministic indexing.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative root class for all SQLAlchemy ORM models in Project EDITH."""
    pass


class User(Base):
    """Represents a unified user identity across WhatsApp, Telegram, and API channels.

    Attributes:
        id: Internal surrogate primary key.
        external_id: Unique cross-platform identifier (e.g. phone number, telegram chat ID,
            or hashed API client token).
        display_name: Human-readable display label or profile name.
        email: Optional administrative contact address.
        is_admin: True if user holds operational management privileges.
        created_at: UTC timestamp of account discovery/registration.
        conversations: Cascading relationship to user conversation threads.
        preferences: One-to-one preference record (preferred model, language, style).
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    conversations: Mapped[list["Conversation"]] = relationship(
        "Conversation", back_populates="user", cascade="all, delete-orphan"
    )
    preferences = relationship(
        "UserPreference", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class Conversation(Base):
    """Represents an ongoing conversational dialogue thread bound to a user and platform chat.

    Attributes:
        id: Primary key identifying this conversation thread.
        user_id: Foreign key reference to the owning User.
        external_chat_id: Platform-specific thread/chat identifier (e.g., JID or Telegram group/user ID).
        title: Optional human-readable or auto-generated topic title.
        conversation_metadata: Optional JSON-encoded contextual parameters.
        created_at: Timestamp when thread was established.
        updated_at: Timestamp of latest message exchange.
        user: Owning User relationship.
        messages: Chronologically ordered messages in this thread (cascade delete).
    """
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    external_chat_id: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=True)
    conversation_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[User] = relationship("User", back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    """Represents an individual message exchanged in a conversation thread.

    Attributes:
        id: Primary key of the message entry.
        conversation_id: Foreign key to the parent conversation thread.
        sender: Sender identifier (e.g., user external_id or 'assistant').
        role: Message role for Ollama chat formatting ('user', 'assistant', 'system', 'tool').
        content: Unsanitized text payload of the message.
        message_type: Payload discriminator ('text', 'image', 'audio', 'document').
        created_at: UTC timestamp when the message was recorded.
        conversation: Parent conversation relationship.
    """
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    sender: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), default="text")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    conversation: Mapped[Conversation] = relationship("Conversation", back_populates="messages")


class MemoryRecord(Base):
    """Stores persistent long-term factual statements, user preferences, and contextual memories.

    Attributes:
        id: Surrogate primary key.
        user_id: Foreign key referencing the owning User.
        key: Case-insensitive unique slug per user (e.g. 'favorite_food', 'project_stack').
        value: Exact memory payload or factual note.
        tags: Optional comma-delimited classification tags for semantic relevance matching.
        created_at: Creation timestamp.
        updated_at: Timestamp of latest value modification.
    """
    __tablename__ = "memory_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_memory_records_user_key", "user_id", "key"),
    )


class InboundMessage(Base):
    """Durable staging queue for inbound messages arriving from external channels.

    Provides idempotency and lease-based processing semantics across WhatsApp, Telegram,
    and HTTP API clients.

    Attributes:
        id: Internal queue sequential ID.
        platform: Ingress channel provider ('whatsapp', 'telegram', 'api').
        external_id: Upstream platform message ID used for duplicate rejection.
        chat_id: External channel chat or thread identifier.
        sender: Sender contact ID or client token hash.
        sender_name: User display name provided by the ingress transport.
        content: Inbound text body.
        message_type: Inbound media type ('text', 'image', 'audio', etc.).
        file_url: Optional remote or local path to incoming attachment.
        mime_type: MIME type of incoming file attachment.
        caption: Media caption accompanying the attachment.
        status: Queue processing status: 'RECEIVED', 'PROCESSING', 'RESPONSE_READY',
            'COMPLETED', 'FAILED'.
        attempt_count: Number of worker processing attempts.
        last_error: Error traceback or diagnostics from the most recent processing attempt.
        response_text: Synthesized assistant response ready for delivery staging.
        lease_expires_at: Expiration timestamp for the current worker processing lease.
        created_at: Message arrival timestamp.
        updated_at: Status transition timestamp.
    """
    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), default="whatsapp", nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    sender: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    message_type: Mapped[str] = mapped_column(String(32), default="text")
    file_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RECEIVED", nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_inbound_platform_external_id"),
        Index("ix_inbound_chat_status", "chat_id", "status"),
    )


class OutboundMessage(Base):
    """Durable delivery queue for outbound assistant messages and responses.

    Guarantees at-least-once delivery with exponential backoff retry, lease locking,
    and ambiguous state protection against transport timeouts.

    Attributes:
        id: Queue sequential ID.
        inbound_id: Optional reference to the triggering inbound message.
        conversation_id: Optional reference to the associated conversation thread.
        message_id: Optional reference to the recorded history Message entry.
        correlation_id: Unique idempotency UUID identifying this delivery attempt.
        platform: Destination messaging platform ('whatsapp', 'telegram', 'api').
        chat_id: Target external chat or recipient ID.
        text: Outbound text body.
        file_url: Optional attachment URL or local path.
        mime_type: MIME type of outbound attachment.
        caption: Outbound media caption.
        status: Delivery state: 'PENDING', 'SENDING', 'SENT', 'AMBIGUOUS', 'FAILED'.
        attempt_count: Total dispatch attempts made.
        max_attempts: Maximum retries allowed before transition to 'FAILED'.
        last_error: Error message from last failed transmission.
        lease_expires_at: Lease expiration timestamp for active dispatcher lock.
        sent_at: Timestamp of verified successful delivery.
        created_at: Queue insertion timestamp.
        updated_at: State update timestamp.
    """
    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    inbound_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True, index=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), default="whatsapp", nullable=False)
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_outbound_chat_status", "chat_id", "status"),
    )


class UserPreference(Base):
    """User-specific execution preferences and operational defaults.

    Attributes:
        id: Primary key.
        user_id: Unique foreign key referencing owning User (one-to-one).
        preferred_model: Model name override (e.g. 'dolphin3:8b').
        preferred_language: Preferred spoken/written response language.
        response_style: Desired verbosity or tone ('concise', 'detailed', 'technical').
        created_at: Record creation timestamp.
        updated_at: Record update timestamp.
        user: Parent user relationship.
    """
    __tablename__ = "user_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    preferred_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    preferred_language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user: Mapped[User] = relationship("User", back_populates="preferences")


class SystemSetting(Base):
    """Global configuration key-value pairs stored in the relational database.

    Used for runtime operational overrides (such as 'default_model') that take precedence
    over environment file defaults without requiring service restarts.

    Attributes:
        key: Setting configuration key (primary key).
        value: String value representation.
        updated_at: Timestamp of latest modification.
    """
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class PendingAction(Base):
    """Stores high-privilege tool invocations awaiting explicit human confirmation.

    Implements two-phase execution safety: dangerous side-effects (file write/delete,
    shell execution) generate a PendingAction with a cryptographically bound token
    and strict TTL. The tool is executed only after the user supplies `/confirm <token>`.

    Attributes:
        id: Primary key.
        user_id: User who triggered the action.
        conversation_id: Thread where the action originated.
        action_type: Classifier for the sensitive action.
        tool_name: Registered tool name to invoke upon confirmation.
        arguments_json: Serialized JSON kwargs for the tool execution.
        summary: Human-readable explanation presented for user review.
        confirmation_token: High-entropy hex token required to authorize execution.
        expires_at: Expiration deadline after which token is invalid.
        status: Lifecycle state: 'PENDING', 'CONSUMED', 'CANCELLED', 'EXPIRED'.
        attempt_count: Number of failed confirmation attempts.
        max_attempts: Maximum failed token guesses before auto-cancellation.
        created_at: Creation timestamp.
        updated_at: Modification timestamp.
    """
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    confirmation_token: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)  # PENDING, CONSUMED, CANCELLED, EXPIRED
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_pending_actions_user_conv_status", "user_id", "conversation_id", "status"),
    )
