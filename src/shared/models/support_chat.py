from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base
from src.shared.models.core import utc_now


class SupportChatRateLimitRecord(Base):
    __tablename__ = "support_chat_rate_limits"

    user_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False)


class SupportChatSessionRecord(Base):
    __tablename__ = "support_chat_sessions"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_support_chat_session_owner"),
        Index("ix_support_chat_sessions_user_updated", "user_id", "updated_at"),
        Index("ix_support_chat_sessions_user_pinned_updated", "user_id", "pinned", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="Cuộc trò chuyện mới")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class SupportChatMessageRecord(Base):
    __tablename__ = "support_chat_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["support_chat_sessions.id", "support_chat_sessions.user_id"],
            name="fk_support_chat_message_owned_session",
            ondelete="CASCADE",
        ),
        Index("ix_support_chat_messages_session_created", "user_id", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    required_permission: Mapped[str | None] = mapped_column(String(100), nullable=True)
    application_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sources_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    actions_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    data_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
