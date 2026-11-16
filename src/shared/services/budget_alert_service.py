from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import Settings, get_settings
from src.shared.models.budget_alert import BudgetAlertDeliveryRecord
from src.shared.models.core import Application
from src.shared.models.gateway import ApplicationGatewayConfig
from src.shared.services.application_budget_service import ApplicationBudgetService
from src.shared.services.email_service import (
    EmailDeliveryError,
    EmailDeliveryNotConfiguredError,
    ResendEmailService,
)
from src.shared.services.supabase_user_service import SupabaseUserService

logger = logging.getLogger("uvicorn.error")


class BudgetAlertService:
    """Send one warning and one exhausted email per application/month/recipient."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    async def notify_if_needed(self, application_id: str) -> None:
        if not self.settings.budget_alert_enabled:
            return

        snapshot = await ApplicationBudgetService(self.session).snapshot(application_id)
        if snapshot.state not in {"warning", "exhausted"} or snapshot.limit_usd is None:
            return

        application = (
            await self.session.execute(
                select(Application)
                .select_from(ApplicationGatewayConfig)
                .join(Application, Application.id == ApplicationGatewayConfig.application_id)
                .where(ApplicationGatewayConfig.application_id == application_id)
            )
        ).scalar_one_or_none()
        if application is None:
            return

        recipients = await self._recipients(owner_user_id=application.owner_user_id)
        if not recipients:
            logger.warning(
                "Budget alert skipped for application %s: no recipient configured",
                application_id,
            )
            return

        alert_level = snapshot.state
        for recipient in recipients:
            delivery = await self._claim_delivery(
                application_id=application_id,
                period_start=snapshot.period_start,
                alert_level=alert_level,
                recipient=recipient,
            )
            if delivery is None:
                continue
            try:
                message_id = await ResendEmailService(self.settings).send(
                    recipient=recipient,
                    subject=self._subject(application.name, alert_level),
                    html=self._html(application.name, snapshot, alert_level),
                    text=self._text(application.name, snapshot, alert_level),
                )
            except (EmailDeliveryError, EmailDeliveryNotConfiguredError) as exc:
                await self._mark_failed(delivery.id, str(exc))
                logger.warning("Budget alert email failed for %s: %s", application_id, exc)
            else:
                await self._mark_sent(delivery.id, message_id)

    async def _recipients(self, *, owner_user_id: str) -> list[str]:
        owner_email = await SupabaseUserService(self.settings).email_for_user(owner_user_id)
        return [owner_email] if owner_email else []

    async def _claim_delivery(
        self,
        *,
        application_id: str,
        period_start: datetime,
        alert_level: str,
        recipient: str,
    ) -> BudgetAlertDeliveryRecord | None:
        existing = await self.session.scalar(
            select(BudgetAlertDeliveryRecord).where(
                BudgetAlertDeliveryRecord.application_id == application_id,
                BudgetAlertDeliveryRecord.period_start == period_start,
                BudgetAlertDeliveryRecord.alert_level == alert_level,
                BudgetAlertDeliveryRecord.recipient == recipient,
            )
        )
        # A pending record may mean the process is between claiming and
        # calling Resend; a failed record may have an ambiguous provider
        # outcome. Never retry automatically because the product requirement
        # is at-most-once email delivery.
        if existing is not None:
            return None
        delivery = existing or BudgetAlertDeliveryRecord(
            application_id=application_id,
            period_start=period_start,
            alert_level=alert_level,
            recipient=recipient,
        )
        delivery.status = "pending"
        delivery.error_message = None
        self.session.add(delivery)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return None
        return delivery

    async def _mark_sent(self, delivery_id: str, message_id: str) -> None:
        delivery = await self.session.get(BudgetAlertDeliveryRecord, delivery_id)
        if delivery is None:
            return
        delivery.status = "sent"
        delivery.provider_message_id = message_id
        delivery.sent_at = datetime.now(UTC)
        await self.session.commit()

    async def _mark_failed(self, delivery_id: str, message: str) -> None:
        delivery = await self.session.get(BudgetAlertDeliveryRecord, delivery_id)
        if delivery is None:
            return
        delivery.status = "failed"
        delivery.error_message = message[:1000]
        await self.session.commit()

    @staticmethod
    def _subject(application_name: str, alert_level: str) -> str:
        if alert_level == "warning":
            return f"[{application_name}] Budget warning · usage threshold reached"
        return f"[{application_name}] Budget exhausted · action required"

    @staticmethod
    def _text(application_name: str, snapshot, alert_level: str) -> str:
        is_warning = alert_level == "warning"
        title = "Monthly budget warning" if is_warning else "Monthly budget exhausted"
        explanation = (
            "Usage has reached the configured warning threshold. Please review current usage and expected activity."
            if is_warning
            else "The application has reached or exceeded its monthly budget. New requests may be blocked according to the configured budget action."
        )
        recommendation = (
            "Review usage and adjust the monthly budget if continued activity is expected."
            if is_warning
            else "Review recent usage, increase the monthly budget if appropriate, or wait for the next UTC budget period."
        )
        period_end = (snapshot.period_end - timedelta(days=1)).date()
        return (
            "AI20K Gateway — Budget notification\n\n"
            f"{title}\n"
            f"{explanation}\n\n"
            "Application details\n"
            f"Application: {application_name}\n"
            f"Budget period (UTC): {snapshot.period_start.date()} to {period_end}\n\n"
            "Usage summary\n"
            f"Monthly budget: ${snapshot.limit_usd:.4f}\n"
            f"Spent: ${snapshot.spent_usd:.4f}\n"
            f"Remaining budget: ${max(snapshot.remaining_usd or 0, 0):.4f}\n"
            f"Overage: ${snapshot.overage_usd:.4f}\n"
            f"Usage: {(snapshot.usage_percent or 0) * 100:.1f}%\n"
            f"Warning threshold: {snapshot.warning_threshold * 100:.1f}%\n"
            f"Gateway requests: {snapshot.requests}\n"
            f"Tokens processed: {snapshot.tokens}\n\n"
            f"Recommended action\n{recommendation}\n\n"
            "This is an automated notification sent to the application owner."
        )

    @staticmethod
    def _html(application_name: str, snapshot, alert_level: str) -> str:
        is_warning = alert_level == "warning"
        title = "Monthly budget warning" if is_warning else "Monthly budget exhausted"
        explanation = (
            "Usage has reached the configured warning threshold. Please review current usage and expected activity."
            if is_warning
            else "The application has reached or exceeded its monthly budget. New requests may be blocked according to the configured budget action."
        )
        recommendation = (
            "Review usage and adjust the monthly budget if continued activity is expected."
            if is_warning
            else "Review recent usage, increase the monthly budget if appropriate, or wait for the next UTC budget period."
        )
        period_end = (snapshot.period_end - timedelta(days=1)).date()
        accent = "#b45309" if is_warning else "#b91c1c"
        background = "#fffbeb" if is_warning else "#fef2f2"
        rows = (
            ("Monthly budget", f"${snapshot.limit_usd:.4f}"),
            ("Spent", f"${snapshot.spent_usd:.4f}"),
            ("Remaining budget", f"${max(snapshot.remaining_usd or 0, 0):.4f}"),
            ("Overage", f"${snapshot.overage_usd:.4f}"),
            ("Usage", f"{(snapshot.usage_percent or 0) * 100:.1f}%"),
            ("Warning threshold", f"{snapshot.warning_threshold * 100:.1f}%"),
            ("Gateway requests", f"{snapshot.requests:,}"),
            ("Tokens processed", f"{snapshot.tokens:,}"),
        )
        summary_rows = "".join(
            f"<tr><td style=\"padding:8px 0;color:#64748b;border-bottom:1px solid #e2e8f0;\">{html.escape(label)}</td>"
            f"<td style=\"padding:8px 0;text-align:right;font-weight:600;color:#0f172a;border-bottom:1px solid #e2e8f0;\">{html.escape(value)}</td></tr>"
            for label, value in rows
        )
        return f"""<!doctype html>
<html>
  <body style="margin:0;background:#f8fafc;color:#0f172a;font-family:Arial,Helvetica,sans-serif;line-height:1.5;">
    <div style="padding:32px 16px;">
      <div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
        <div style="padding:24px 28px;background:{background};border-bottom:1px solid #e2e8f0;">
          <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#64748b;font-weight:700;">AI20K Gateway</div>
          <h1 style="margin:8px 0 6px;font-size:24px;line-height:1.25;color:{accent};">{html.escape(title)}</h1>
          <p style="margin:0;color:#475569;">{html.escape(explanation)}</p>
        </div>
        <div style="padding:24px 28px;">
          <p style="margin:0 0 18px;color:#334155;">Application: <strong>{html.escape(application_name)}</strong></p>
          <p style="margin:0 0 18px;color:#64748b;font-size:14px;">Budget period (UTC): {snapshot.period_start.date()} to {period_end}</p>
          <h2 style="margin:0 0 8px;font-size:16px;color:#0f172a;">Usage summary</h2>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{summary_rows}</table>
          <div style="margin-top:24px;padding:16px;background:#f8fafc;border-radius:8px;">
            <strong style="display:block;margin-bottom:4px;color:#0f172a;">Recommended action</strong>
            <span style="color:#475569;">{html.escape(recommendation)}</span>
          </div>
          <p style="margin:24px 0 0;color:#94a3b8;font-size:12px;">This is an automated notification sent to the application owner.</p>
        </div>
      </div>
    </div>
  </body>
</html>"""


__all__ = ["BudgetAlertService"]
