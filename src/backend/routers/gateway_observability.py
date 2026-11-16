from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.gateway_observability import GatewayOverviewResponse, UsagePageResponse
from src.backend.dtos.incidents import IncidentCreate, IncidentPage, IncidentRead, IncidentReportCreate, IncidentUpdate
from src.backend.services.incident_service import IncidentNotFoundError, IncidentService, IncidentValidationError
from src.backend.services.observability_service import (
    ObservabilityPermissionError,
    ObservabilityService,
    ObservabilityValidationError,
    application_identity,
)
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user, require_any_permission, require_permission
from src.shared.models.core import Application
from src.shared.models.runtime import GatewayRequestRecord, ModelExecutionRecord, StageTraceRecord
from src.shared.services.gateway_persistence_service import GatewayPersistenceService
from src.shared.services.gateway_runtime_service import GatewayRuntimeService

router = APIRouter(prefix="/gateway", tags=["gateway-observability"])
AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
RequestReadUser = Annotated[CurrentUser, Depends(require_any_permission("requests.read_own", "requests.read_global", "usage.read_own", "usage.read_global"))]


def require_incident_reporter(user: AuthUser) -> CurrentUser:
    if "user" not in user.roles or "admin" in user.roles or "platform_admin" in user.roles:
        raise HTTPException(status_code=403, detail="Incident reports are only available to users")
    return user


IncidentReporter = Annotated[CurrentUser, Depends(require_incident_reporter)]


async def _owned_application(session: AsyncSession, user_id: str, application_id: str) -> Application:
    application = await session.scalar(
        select(Application).where(
            Application.id == application_id,
            Application.owner_user_id == user_id,
        )
    )
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return application


async def _visible_request(
    session: AsyncSession,
    user_id: str,
    request_id: str,
    *,
    can_read_global: bool,
) -> tuple[GatewayRequestRecord, str, bool]:
    identity = application_identity()
    ownership = [] if can_read_global else [identity.c.owner_user_id == user_id]
    row = (
        await session.execute(
            select(GatewayRequestRecord, identity.c.name, identity.c.application_deleted)
            .join(identity, identity.c.application_id == GatewayRequestRecord.application_id)
            .where(
                GatewayRequestRecord.request_id == request_id,
                *ownership,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Gateway request not found")
    record, application_name, application_deleted = row
    return record, application_name, application_deleted


def _page(page: int, page_size: int, total: int, items: list) -> dict:
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@router.get("/requests")
async def list_requests(
    user: RequestReadUser,
    session: DbSession,
    application_id: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=150),
    status_filter: str | None = Query(default=None, alias="status", pattern=r"^(pending|completed|partial|failed)$"),
    source: str | None = Query(default=None, pattern=r"^(playground|api|unknown)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None),
) -> dict:
    can_read_global = "requests.read_global" in user.permissions or "usage.read_global" in user.permissions
    identity = application_identity()
    conditions = [] if can_read_global else [identity.c.owner_user_id == user.id]
    if application_id:
        conditions.append(GatewayRequestRecord.application_id == application_id)
    if search:
        term = f"%{search.strip()}%"
        conditions.append(identity.c.name.ilike(term))
    if status_filter:
        conditions.append(GatewayRequestRecord.status == status_filter)
    if source:
        conditions.append(GatewayRequestRecord.source == source)
    total = int(await session.scalar(
        select(func.count())
        .select_from(GatewayRequestRecord)
        .join(identity, identity.c.application_id == GatewayRequestRecord.application_id)
        .where(*conditions)
    ) or 0)
    sort_columns = {
        "started_at": GatewayRequestRecord.started_at,
        "latency": GatewayRequestRecord.total_latency_ms,
        "cost": GatewayRequestRecord.total_cost,
        "status": GatewayRequestRecord.status,
        "source": GatewayRequestRecord.source,
    }
    requested_fields = list(dict.fromkeys(field for field in (sort_by or "").split(",") if field in sort_columns))
    requested_dirs = [direction for direction in (sort_dir or "").split(",") if direction in {"asc", "desc"}]
    ordering = [
        sort_columns[field].desc() if (requested_dirs[index] if index < len(requested_dirs) else "asc") == "desc" else sort_columns[field].asc()
        for index, field in enumerate(requested_fields)
    ]
    ordering.append(GatewayRequestRecord.request_id.asc())
    rows = list(await session.execute(
        select(GatewayRequestRecord, identity.c.name, identity.c.application_deleted)
        .join(identity, identity.c.application_id == GatewayRequestRecord.application_id)
        .where(*conditions)
        .order_by(*ordering)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ))
    return _page(
        page,
        page_size,
        total,
        [_request_item(row, application_name, application_deleted) for row, application_name, application_deleted in rows],
    )


@router.get("/requests/{request_id}")
async def get_request(request_id: str, user: RequestReadUser, session: DbSession) -> dict:
    can_read_global = "requests.read_global" in user.permissions or "usage.read_global" in user.permissions
    record, application_name, application_deleted = await _visible_request(
        session, user.id, request_id, can_read_global=can_read_global
    )
    persistence = GatewayPersistenceService(session)
    return await GatewayRuntimeService(session).request_detail(
        record,
        application_name=application_name,
        application_deleted=application_deleted,
        tasks=await persistence.list_tasks(request_id),
        aggregation=await persistence.get_aggregation(request_id),
        traces=await persistence.list_traces(request_id),
        executions=await persistence.list_executions(request_id),
    )


@router.get("/requests/{request_id}/traces")
async def get_traces(
    request_id: str, user: RequestReadUser, session: DbSession,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
) -> dict:
    can_read_global = "requests.read_global" in user.permissions or "usage.read_global" in user.permissions
    await _visible_request(session, user.id, request_id, can_read_global=can_read_global)
    condition = StageTraceRecord.request_id == request_id
    total = int(await session.scalar(select(func.count()).select_from(StageTraceRecord).where(condition)) or 0)
    rows = list(await session.scalars(
        select(StageTraceRecord).where(condition).order_by(StageTraceRecord.started_at, StageTraceRecord.id)
        .offset((page - 1) * page_size).limit(page_size)
    ))
    return _page(page, page_size, total, [_model_item(item) for item in rows])


@router.get("/requests/{request_id}/executions")
async def get_executions(
    request_id: str, user: RequestReadUser, session: DbSession,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
) -> dict:
    can_read_global = "requests.read_global" in user.permissions or "usage.read_global" in user.permissions
    await _visible_request(session, user.id, request_id, can_read_global=can_read_global)
    condition = ModelExecutionRecord.request_id == request_id
    total = int(await session.scalar(select(func.count()).select_from(ModelExecutionRecord).where(condition)) or 0)
    rows = list(await session.scalars(
        select(ModelExecutionRecord).where(condition).order_by(ModelExecutionRecord.id)
        .offset((page - 1) * page_size).limit(page_size)
    ))
    return _page(page, page_size, total, [_model_item(item) for item in rows])


@router.get("/summary")
async def summary(user: AuthUser, session: DbSession) -> dict:
    identity = application_identity()
    owned = GatewayRequestRecord.application_id.in_(
        select(identity.c.application_id).where(identity.c.owner_user_id == user.id)
    )
    totals = (await session.execute(
        select(
            func.count(GatewayRequestRecord.request_id),
            func.coalesce(func.sum(case((GatewayRequestRecord.status == "completed", 1), else_=0)), 0),
            func.coalesce(func.sum(case((GatewayRequestRecord.status == "partial", 1), else_=0)), 0),
            func.coalesce(func.sum(case((GatewayRequestRecord.status == "failed", 1), else_=0)), 0),
            func.coalesce(func.sum(GatewayRequestRecord.total_cost), 0),
        ).where(owned)
    )).one()
    item = {
        "total_requests": int(totals[0] or 0),
        "completed_requests": int(totals[1] or 0),
        "partial_requests": int(totals[2] or 0),
        "failed_requests": int(totals[3] or 0),
        "total_cost": round(float(totals[4] or 0), 10),
    }
    return _page(1, 1, 1, [item])


@router.get("/overview", response_model=GatewayOverviewResponse)
async def overview(
    user: AuthUser,
    session: DbSession,
    application_id: str | None = Query(default=None),
    scope: str = Query(default="own", pattern=r"^(own|global)$"),
    from_value: str | None = Query(default=None, alias="from"),
    to_value: str | None = Query(default=None, alias="to"),
) -> GatewayOverviewResponse:
    try:
        return await ObservabilityService(session).overview(
            user.id,
            application_id=application_id,
            scope=scope,
            can_read_global="usage.read_global" in user.permissions,
            from_value=from_value,
            to_value=to_value,
        )
    except ObservabilityPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ObservabilityValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/usage", response_model=UsagePageResponse)
async def usage(
    user: AuthUser,
    session: DbSession,
    application_id: str | None = Query(default=None),
    model: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    provider_id: str | None = Query(default=None),
    source: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    scope: str = Query(default="own", pattern=r"^(own|global)$"),
    from_value: str | None = Query(default=None, alias="from"),
    to_value: str | None = Query(default=None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> UsagePageResponse:
    try:
        return await ObservabilityService(session).usage(
            user.id,
            application_id=application_id,
            model=model,
            model_id=model_id,
            provider=provider,
            provider_id=provider_id,
            source=source,
            status=status_filter,
            scope=scope,
            can_read_global="usage.read_global" in user.permissions,
            from_value=from_value,
            to_value=to_value,
            page=page,
            page_size=page_size,
        )
    except ObservabilityPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ObservabilityValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/usage/export.csv")
async def export_usage(
    user: AuthUser,
    session: DbSession,
    application_id: str | None = Query(default=None),
    model: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    provider_id: str | None = Query(default=None),
    source: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    scope: str = Query(default="own", pattern=r"^(own|global)$"),
    from_value: str | None = Query(default=None, alias="from"),
    to_value: str | None = Query(default=None, alias="to"),
) -> Response:
    try:
        page = await ObservabilityService(session).usage(
            user.id,
            application_id=application_id,
            model=model,
            model_id=model_id,
            provider=provider,
            provider_id=provider_id,
            source=source,
            status=status_filter,
            scope=scope,
            can_read_global="usage.read_global" in user.permissions,
            from_value=from_value,
            to_value=to_value,
            page=1,
            page_size=100000,
        )
    except ObservabilityPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ObservabilityValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "request_id",
            "timestamp",
            "application_id",
            "application",
            "application_deleted",
            "model",
            "provider",
            "source",
            "status",
            "tokens",
            "latency_ms",
            "cost",
            "fallback_used",
        ],
    )
    writer.writeheader()
    for row in page.items:
        writer.writerow(row.model_dump(mode="json"))
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage.csv"},
    )


@router.get("/incidents", response_model=IncidentPage)
async def list_incidents(
    user: Annotated[CurrentUser, Depends(require_any_permission("incidents.read", "incidents.manage"))],
    session: DbSession,
    application_id: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> IncidentPage:
    return await IncidentService(session).list(
        user.id,
        application_id=application_id,
        severity=severity,
        status=status_filter,
        search=search,
        page=page,
        page_size=page_size,
        can_read_global=True,
    )


@router.get("/incident-reports", response_model=IncidentPage)
async def list_incident_reports(
    user: IncidentReporter,
    session: DbSession,
    application_id: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> IncidentPage:
    return await IncidentService(session).list(
        user.id, reports_only=True, application_id=application_id, severity=severity, status=status_filter,
        search=search, page=page, page_size=page_size,
    )


@router.post("/incident-reports", response_model=IncidentRead, status_code=status.HTTP_201_CREATED)
async def report_incident(payload: IncidentReportCreate, user: IncidentReporter, session: DbSession) -> IncidentRead:
    try:
        return await IncidentService(session).report(user.id, user.email, payload)
    except IncidentValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/incidents", response_model=IncidentRead, status_code=status.HTTP_201_CREATED)
async def create_incident(
    payload: IncidentCreate,
    user: Annotated[CurrentUser, Depends(require_permission("incidents.manage"))],
    session: DbSession,
) -> IncidentRead:
    try:
        return await IncidentService(session).create(user.id, payload, can_manage_global=True)
    except IncidentValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/incidents/{incident_id}", response_model=IncidentRead)
async def update_incident(
    incident_id: str,
    payload: IncidentUpdate,
    user: Annotated[CurrentUser, Depends(require_permission("incidents.manage"))],
    session: DbSession,
) -> IncidentRead:
    try:
        return await IncidentService(session).update(user.id, incident_id, payload, can_manage_global=True)
    except IncidentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IncidentValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/status")
async def gateway_status(_: AuthUser, session: DbSession) -> dict:
    await session.execute(select(func.count()).select_from(Application))
    return {
        "status": "ok",
        "database": session.get_bind().dialect.name,
        "execution_mode": get_settings().gateway_execution_mode,
    }


def _request_item(
    row: GatewayRequestRecord,
    application_name: str | None = None,
    application_deleted: bool = False,
) -> dict:
    return {
        "request_id": row.request_id,
        "application_id": row.application_id,
        "application_name": application_name,
        "application_deleted": application_deleted,
        "source": row.source,
        "status": row.status,
        "aggregation_status": row.aggregation_status,
        "total_cost": row.total_cost,
        "total_latency_ms": row.total_latency_ms,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def _model_item(item) -> dict | None:
    if item is None:
        return None
    return {key: value for key, value in vars(item).items() if not key.startswith("_")}
