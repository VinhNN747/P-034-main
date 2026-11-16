from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, distinct, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.application_budget import ApplicationBudgetSummary
from src.backend.dtos.gateway_observability import (
    GatewayOverviewResponse,
    UsagePageResponse,
    UsageRowResponse,
    UsageSummaryResponse,
)
from src.shared.models.core import Application, DeletedApplication
from src.shared.models.gateway import Model
from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord
from src.shared.services.application_budget_service import ApplicationBudgetService


def application_identity():
    """Return the live or tombstoned identity used to scope retained history."""
    return union_all(
        select(
            Application.id.label("application_id"),
            Application.owner_user_id.label("owner_user_id"),
            Application.name.label("name"),
            literal(False).label("application_deleted"),
        ),
        select(
            DeletedApplication.application_id.label("application_id"),
            DeletedApplication.owner_user_id.label("owner_user_id"),
            DeletedApplication.name.label("name"),
            literal(True).label("application_deleted"),
        ),
    ).subquery("application_identity")


class ObservabilityPermissionError(Exception):
    pass


class ObservabilityValidationError(Exception):
    pass


class ObservabilityService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def overview(
        self,
        user_id: str,
        *,
        application_id: str | None = None,
        scope: str = "own",
        can_read_global: bool = False,
        from_value: str | None = None,
        to_value: str | None = None,
    ) -> GatewayOverviewResponse:
        start, end = self._date_range(from_value, to_value)
        self._check_scope(scope, can_read_global)
        conditions = self._request_conditions(
            user_id, application_id=application_id, scope=scope, can_read_global=can_read_global, start=start, end=end
        )
        identity = application_identity()
        totals = (await self.session.execute(
            select(
                func.count(GatewayRequestRecord.request_id),
                func.coalesce(func.sum(case((GatewayRequestRecord.status == "completed", 1), else_=0)), 0),
                func.coalesce(func.avg(GatewayRequestRecord.total_latency_ms), 0),
                func.coalesce(func.sum(GatewayRequestRecord.total_cost), 0),
            ).where(*conditions)
        )).one()
        total, completed, average_latency, total_cost = (int(totals[0] or 0), int(totals[1] or 0), float(totals[2] or 0), float(totals[3] or 0))
        active_model_count = int(
            await self.session.scalar(select(func.count()).select_from(Model).where(Model.status == "active")) or 0
        )
        recent_rows = list(await self.session.execute(
            select(GatewayRequestRecord, identity.c.name, identity.c.application_deleted)
            .join(identity, identity.c.application_id == GatewayRequestRecord.application_id)
            .where(*conditions)
            .order_by(GatewayRequestRecord.started_at.desc(), GatewayRequestRecord.request_id.desc())
            .limit(5)
        ))
        signals = [
            {
                "request_id": row.request_id,
                "application_id": row.application_id,
                "application_name": application_name,
                "application_deleted": application_deleted,
                "status": row.status,
                "source": row.source,
                "latency_ms": row.total_latency_ms,
                "cost": row.total_cost,
                "timestamp": row.started_at,
            }
            for row, application_name, application_deleted in recent_rows
        ]
        return GatewayOverviewResponse(
            total_requests=total,
            success_rate=round(completed / total, 4) if total else 0.0,
            average_latency_ms=round(average_latency, 3),
            total_cost=round(total_cost, 10),
            active_model_count=active_model_count,
            recent_signals=signals,
            **{"from": from_value, "to": to_value},
        )

    async def usage(
        self,
        user_id: str,
        *,
        application_id: str | None = None,
        model: str | None = None,
        model_id: str | None = None,
        provider: str | None = None,
        provider_id: str | None = None,
        source: str | None = None,
        status: str | None = None,
        scope: str = "own",
        can_read_global: bool = False,
        from_value: str | None = None,
        to_value: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> UsagePageResponse:
        start, end = self._date_range(from_value, to_value)
        self._check_scope(scope, can_read_global)
        conditions = self._usage_conditions(
            user_id, application_id=application_id, model=model or model_id, provider=provider or provider_id,
            source=source, status=status, scope=scope, can_read_global=can_read_global, start=start, end=end,
        )
        aggregates = (await self.session.execute(
            select(
                func.count(UsageEventRecord.id),
                func.count(distinct(UsageEventRecord.request_id)),
                func.coalesce(func.sum(UsageEventRecord.input_tokens + UsageEventRecord.output_tokens), 0),
                func.coalesce(func.sum(UsageEventRecord.cost_usd), 0),
            )
            .join(GatewayRequestRecord, GatewayRequestRecord.request_id == UsageEventRecord.request_id)
            .where(*conditions)
        )).one()
        total, request_count, token_count, total_cost = (
            int(aggregates[0] or 0), int(aggregates[1] or 0), int(aggregates[2] or 0), float(aggregates[3] or 0)
        )
        identity = application_identity()
        selected = (await self.session.execute(
            select(
                UsageEventRecord,
                GatewayRequestRecord,
                identity.c.name,
                identity.c.application_deleted,
            )
            .join(GatewayRequestRecord, GatewayRequestRecord.request_id == UsageEventRecord.request_id)
            .join(identity, identity.c.application_id == UsageEventRecord.application_id)
            .where(*conditions)
            .order_by(UsageEventRecord.created_at.desc(), UsageEventRecord.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )).all()
        response_rows = [
            self._usage_row(event, request, application_name, application_deleted)
            for event, request, application_name, application_deleted in selected
        ]
        budget = None
        if application_id:
            budget = ApplicationBudgetSummary.model_validate(
                (await ApplicationBudgetService(self.session).snapshot(application_id)).as_dict()
            )
        return UsagePageResponse(
            items=response_rows,
            page=page,
            page_size=page_size,
            total=total,
            summary=UsageSummaryResponse(
                requests=request_count,
                total_tokens=token_count,
                total_cost=round(total_cost, 10),
            ),
            budget=budget,
        )

    def _request_conditions(
        self, user_id: str, *, application_id: str | None, scope: str, can_read_global: bool,
        start: datetime | None, end: datetime | None,
    ) -> list[Any]:
        conditions = self._scope_conditions(user_id, scope, can_read_global)
        if application_id:
            conditions.append(GatewayRequestRecord.application_id == application_id)
        if start:
            conditions.append(GatewayRequestRecord.started_at >= start)
        if end:
            conditions.append(GatewayRequestRecord.started_at <= end)
        return conditions

    def _usage_conditions(
        self, user_id: str, *, application_id: str | None, model: str | None, provider: str | None,
        source: str | None, status: str | None, scope: str, can_read_global: bool,
        start: datetime | None, end: datetime | None,
    ) -> list[Any]:
        conditions = self._usage_scope_conditions(user_id, scope, can_read_global)
        if application_id:
            conditions.append(UsageEventRecord.application_id == application_id)
        if model:
            # Database routing persists catalog IDs; the usage UI also accepts
            # human-readable model names and historical name-based events.
            conditions.append(or_(
                UsageEventRecord.model == model,
                UsageEventRecord.model.in_(select(Model.id).where(Model.name == model)),
                UsageEventRecord.model.in_(select(Model.name).where(Model.id == model)),
            ))
        if provider:
            conditions.append(UsageEventRecord.provider == provider)
        if source:
            conditions.append(UsageEventRecord.source == source)
        if status:
            conditions.append(GatewayRequestRecord.status == status)
        if start:
            conditions.append(UsageEventRecord.created_at >= start)
        if end:
            conditions.append(UsageEventRecord.created_at <= end)
        return conditions

    @staticmethod
    def _usage_row(
        event: UsageEventRecord,
        request: GatewayRequestRecord,
        application_name: str,
        application_deleted: bool,
    ) -> UsageRowResponse:
        return UsageRowResponse(
            request_id=event.request_id,
            timestamp=event.created_at,
            application_id=event.application_id,
            application=application_name,
            application_deleted=application_deleted,
            model=event.model,
            provider=event.provider,
            source=event.source,
            status=request.status,
            tokens=event.input_tokens + event.output_tokens,
            latency_ms=request.total_latency_ms,
            cost=event.cost_usd,
            fallback_used=request.fallback_used,
        )

    @staticmethod
    def _check_scope(scope: str, can_read_global: bool) -> None:
        if scope not in {"own", "global"}:
            raise ObservabilityValidationError("scope must be own or global")
        if scope == "global" and not can_read_global:
            raise ObservabilityPermissionError("Global usage permission is required")

    @staticmethod
    def _scope_conditions(user_id: str, scope: str, can_read_global: bool) -> list[Any]:
        identity = application_identity()
        if scope == "global" and can_read_global:
            return []
        return [
            GatewayRequestRecord.application_id.in_(
                select(identity.c.application_id).where(identity.c.owner_user_id == user_id)
            )
        ]

    @staticmethod
    def _usage_scope_conditions(user_id: str, scope: str, can_read_global: bool) -> list[Any]:
        identity = application_identity()
        if scope == "global" and can_read_global:
            return []
        return [
            UsageEventRecord.application_id.in_(
                select(identity.c.application_id).where(identity.c.owner_user_id == user_id)
            )
        ]

    @staticmethod
    def _date_range(from_value: str | None, to_value: str | None) -> tuple[datetime | None, datetime | None]:
        start = ObservabilityService._parse_date(from_value)
        end = ObservabilityService._parse_date(to_value)
        if start and end and start > end:
            raise ObservabilityValidationError("from must be earlier than or equal to to")
        return start, end

    @staticmethod
    def _parse_date(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ObservabilityValidationError("date filters must be ISO-8601 values") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
