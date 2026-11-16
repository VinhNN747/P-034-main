from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.gateway import ApplicationGatewayConfig
from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord

BudgetState = Literal["not_configured", "healthy", "warning", "exhausted"]


@dataclass(frozen=True)
class ApplicationBudgetSnapshot:
    application_id: str
    period_start: datetime
    period_end: datetime
    limit_usd: float | None
    spent_usd: float
    remaining_usd: float | None
    overage_usd: float
    usage_percent: float | None
    warning_threshold: float
    budget_action: str
    requests: int
    tokens: int
    state: BudgetState

    def as_dict(self) -> dict:
        values = asdict(self)
        values["period_start"] = self.period_start.isoformat()
        values["period_end"] = self.period_end.isoformat()
        return values


class BudgetExceededError(Exception):
    def __init__(self, snapshot: ApplicationBudgetSnapshot):
        self.snapshot = snapshot
        code = "BUDGET_APPROVAL_REQUIRED" if snapshot.budget_action == "approval" else "BUDGET_EXCEEDED"
        self.code = code
        super().__init__(
            "Application budget is exhausted and requires approval."
            if code == "BUDGET_APPROVAL_REQUIRED"
            else "Application monthly budget is exhausted."
        )


class ApplicationBudgetService:
    """Monthly application budget computed from the persisted runtime ledger.

    Billing remains a separate, owner-level mock credit ledger. Budget is an
    application-level operating limit and uses gateway request cost as its
    source of truth so pipeline-stage costs are included.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def snapshot(
        self,
        application_id: str,
        *,
        now: datetime | None = None,
    ) -> ApplicationBudgetSnapshot:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        period_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year + 1, month=1)
        else:
            period_end = period_start.replace(month=period_start.month + 1)

        config = await self.session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application_id)
        )
        limit = float(config.budget_ceiling) if config and config.budget_ceiling is not None else None
        warning_threshold = float(config.warning_threshold) if config else 0.8
        budget_action = config.budget_action if config else "block"

        request_totals = (
            await self.session.execute(
                select(
                    func.count(GatewayRequestRecord.request_id),
                    func.coalesce(func.sum(GatewayRequestRecord.total_cost), 0),
                ).where(
                    GatewayRequestRecord.application_id == application_id,
                    GatewayRequestRecord.started_at >= period_start,
                    GatewayRequestRecord.started_at < period_end,
                )
            )
        ).one()
        requests = int(request_totals[0] or 0)
        spent = max(0.0, float(request_totals[1] or 0.0))
        tokens = int(
            await self.session.scalar(
                select(
                    func.coalesce(
                        func.sum(UsageEventRecord.input_tokens + UsageEventRecord.output_tokens),
                        0,
                    )
                ).where(
                    UsageEventRecord.application_id == application_id,
                    UsageEventRecord.created_at >= period_start,
                    UsageEventRecord.created_at < period_end,
                )
            )
            or 0
        )

        if limit is None:
            state: BudgetState = "not_configured"
            remaining = None
            overage = 0.0
            usage_percent = None
        else:
            remaining = limit - spent
            overage = max(0.0, -remaining)
            usage_percent = (spent / limit) if limit > 0 else 1.0
            if spent >= limit:
                state = "exhausted"
            elif usage_percent >= warning_threshold:
                state = "warning"
            else:
                state = "healthy"

        return ApplicationBudgetSnapshot(
            application_id=application_id,
            period_start=period_start,
            period_end=period_end,
            limit_usd=limit,
            spent_usd=spent,
            remaining_usd=remaining,
            overage_usd=overage,
            usage_percent=usage_percent,
            warning_threshold=warning_threshold,
            budget_action=budget_action,
            requests=requests,
            tokens=tokens,
            state=state,
        )

    async def ensure_can_execute(
        self,
        application_id: str,
        *,
        now: datetime | None = None,
    ) -> ApplicationBudgetSnapshot:
        snapshot = await self.snapshot(application_id, now=now)
        if snapshot.state == "exhausted":
            raise BudgetExceededError(snapshot)
        return snapshot


__all__ = [
    "ApplicationBudgetService",
    "ApplicationBudgetSnapshot",
    "BudgetExceededError",
]
