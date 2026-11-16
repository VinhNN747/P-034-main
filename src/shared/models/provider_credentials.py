from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base
from src.shared.models.core import utc_now


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_provider_credentials_version_positive"),
        CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_provider_credentials_status"
        ),
        CheckConstraint(
            "last_test_status IN ('untested', 'healthy', 'failed', 'unavailable', 'skipped')",
            name="ck_provider_credentials_last_test_status",
        ),
        CheckConstraint(
            "last_test_latency_ms IS NULL OR last_test_latency_ms >= 0",
            name="ck_provider_credentials_last_test_latency_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_version: Mapped[str] = mapped_column(String(20), nullable=False, default="fernet-v1")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    key_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_test_status: Mapped[str] = mapped_column(String(20), nullable=False, default="untested")
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_credential_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_test_model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    last_test_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_test_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    def __repr__(self) -> str:
        return f"ProviderCredential(id={self.id!r}, provider_id={self.provider_id!r}, version={self.version!r}, status={self.status!r})"


class ServiceProviderBinding(Base):
    __tablename__ = "service_provider_bindings"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('internal_openai', 'ocr_google', 'routing_jev')",
            name="ck_service_provider_bindings_purpose",
        ),
    )

    purpose: Mapped[str] = mapped_column(String(40), primary_key=True)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
