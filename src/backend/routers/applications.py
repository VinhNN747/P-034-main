from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.application_budget import ApplicationBudgetSummary
from src.backend.dtos.applications import (
    ApplicationCreate,
    ApplicationModelsRead,
    ApplicationPage,
    ApplicationRead,
    ApplicationUpdate,
)
from src.backend.services.application_service import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationService,
    ApplicationStateError,
)
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, require_any_permission, require_permission

router = APIRouter(prefix="/applications", tags=["applications"])
ReadUser = Annotated[CurrentUser, Depends(require_permission("applications.read_own"))]
ListUser = Annotated[CurrentUser, Depends(require_any_permission("applications.read_own", "applications.read_global"))]
WriteUser = Annotated[CurrentUser, Depends(require_any_permission("applications.write_own", "applications.manage_own"))]
ManageUser = Annotated[CurrentUser, Depends(require_permission("applications.manage_own"))]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=ApplicationPage)
async def list_applications(
    current_user: ListUser,
    session: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=100),
    environment: str | None = Query(default=None),
    status: str | None = Query(default=None),
    scope: str = Query(default="own", pattern=r"^(own|global)$"),
    sort_by: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None),
) -> ApplicationPage:
    if scope == "global" and "applications.read_global" not in current_user.permissions:
        raise HTTPException(status_code=403, detail="Insufficient role")
    return await ApplicationService(session).list(
        current_user.id,
        page=page,
        page_size=page_size,
        search=search,
        environment=environment,
        status=status,
        scope=scope,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@router.post("", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED)
async def create_application(
    payload: ApplicationCreate, current_user: WriteUser, session: DbSession
) -> ApplicationRead:
    try:
        return await ApplicationService(session).create(current_user.id, payload)
    except ApplicationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{application_id}", response_model=ApplicationRead)
async def get_application(application_id: str, current_user: ReadUser, session: DbSession) -> ApplicationRead:
    try:
        return await ApplicationService(session).get(current_user.id, application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{application_id}/models", response_model=ApplicationModelsRead)
async def list_application_models(
    application_id: str, current_user: ReadUser, session: DbSession
) -> ApplicationModelsRead:
    try:
        return await ApplicationService(session).models(current_user.id, application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{application_id}/budget", response_model=ApplicationBudgetSummary)
async def get_application_budget(
    application_id: str, current_user: ReadUser, session: DbSession
) -> ApplicationBudgetSummary:
    try:
        application = await ApplicationService(session).get(current_user.id, application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Application not found") from exc
    if application.gateway_config is None or application.budget is None:
        raise HTTPException(status_code=404, detail="Application budget is not configured")
    return application.budget


@router.patch("/{application_id}", response_model=ApplicationRead)
async def update_application(
    application_id: str,
    payload: ApplicationUpdate,
    current_user: WriteUser,
    session: DbSession,
) -> ApplicationRead:
    try:
        return await ApplicationService(session).update(current_user.id, application_id, payload)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApplicationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ApplicationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{application_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_application(application_id: str, current_user: ManageUser, session: DbSession) -> Response:
    try:
        await ApplicationService(session).delete(current_user.id, application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApplicationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
