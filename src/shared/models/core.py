from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_applications_slug"),
        Index("uq_applications_name", "name", unique=True),
        Index("ix_applications_owner_user_id", "owner_user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="development")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # "online" (Applications) or "offline" (Offline pipelines): picks the pipeline per request,
    # see src/backend_ai/gateway/pipeline_profile.py. Fixed at creation; never converted.
    pipeline: Mapped[str] = mapped_column(String(20), nullable=False, default="online", server_default="online")
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class DeletedApplication(Base):
    __tablename__ = "deleted_applications"
    __table_args__ = (Index("ix_deleted_applications_owner_user_id", "owner_user_id"),)

    application_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
