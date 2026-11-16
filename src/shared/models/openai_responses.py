"""Public response retention is independent from operation/billing receipts."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base


class StoredOpenAIResponse(Base):
    __tablename__ = 'gateway_openai_responses'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    history_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
