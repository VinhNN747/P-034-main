from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base
from src.shared.models.core import utc_now


class GatewayFileRecord(Base):
    """A file uploaded through the OpenAI-compatible Files API.

    Owned by one application (never by a conversation: the gateway stays
    stateless); the bytes live in the configured storage backend under
    ``storage_key``. Rows are soft-deleted (``deleted_at``) so audit and
    billing references survive the bytes.
    """

    __tablename__ = "gateway_files"
    __table_args__ = (
        Index("ix_gateway_files_application_created", "application_id", "created_at"),
        Index("ix_gateway_files_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(20), nullable=False, default="active", server_default="active")


class FileQuotaReservation(Base):
    __tablename__ = 'gateway_file_reservations'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), index=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    file_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    storage_backend: Mapped[str] = mapped_column(String(16), default="local", server_default="local")
    state: Mapped[str] = mapped_column(String(16), default="writing", server_default="writing")


class FileDeletionOutbox(Base):
    __tablename__ = 'gateway_file_deletion_outbox'
    file_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    storage_backend: Mapped[str] = mapped_column(String(16))
    storage_key: Mapped[str] = mapped_column(String(512))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    attempts: Mapped[int] = mapped_column(default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
