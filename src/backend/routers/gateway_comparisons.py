import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.dtos.comparison import ComparisonPage, ComparisonRead
from src.shared.services.comparison_query_service import ComparisonNotFoundError, ComparisonQueryService

router = APIRouter(prefix="/gateway/comparisons", tags=["gateway-comparisons"])
AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=ComparisonPage)
async def list_comparisons(
    user: AuthUser,
    session: DbSession,
    application_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ComparisonPage:
    return await ComparisonQueryService(session).list_owned(
        user.id,
        application_id=application_id,
        page=page,
        page_size=page_size,
    )


@router.get("/report/summary")
async def comparison_report(
    user: AuthUser,
    session: DbSession,
    application_id: str | None = Query(default=None),
) -> dict:
    return await ComparisonQueryService(session).report_owned(user.id, application_id)


@router.get("/export.csv")
async def export_comparisons(
    user: AuthUser,
    session: DbSession,
    application_id: str | None = Query(default=None),
) -> Response:
    rows = await ComparisonQueryService(session).export_owned(user.id, application_id)
    fieldnames = [
        "comparison_id",
        "created_at",
        "application_id",
        "source",
        "execution_mode",
        "status",
        "reference_model_id",
        "reference_model_name",
        "gateway_cost_usd",
        "reference_cost_usd",
        "cost_delta_usd",
        "cost_delta_percent",
        "gateway_latency_ms",
        "reference_latency_ms",
        "latency_delta_ms",
        "latency_delta_percent",
        "gateway_quality",
        "reference_quality",
        "quality_delta_points",
        "cost_comparable",
        "latency_comparable",
        "quality_comparable",
        "comparison_spend_usd",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content=stream.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="gateway-comparisons.csv"'},
    )


@router.get("/{comparison_id}", response_model=ComparisonRead)
async def get_comparison(comparison_id: str, user: AuthUser, session: DbSession) -> ComparisonRead:
    try:
        return await ComparisonQueryService(session).get_owned(comparison_id, user.id)
    except ComparisonNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
