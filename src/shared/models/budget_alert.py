from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base
from src.shared.models.core import utc_now


class BudgetAlertDeliveryRecord(Base):
    """Idempotency record for monthly application budget alert emails."""

    __tablename__ = "budget_alert_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "period_start",
            "alert_level",
            "recipient",
            name="uq_budget_alert_delivery_key",
        ),
        Index("ix_budget_alert_deliveries_application_period", "application_id", "period_start"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    alert_level: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


__all__ = ["BudgetAlertDeliveryRecord"]
