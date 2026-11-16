"""Offline pipelines: self-hosted models behind their own off_ API key (see OfflinePipelineService).

API keys are managed with the application key endpoints (/applications/{id}/api-keys): an offline pipeline is an
application with pipeline = "offline", so its keys get the off_ prefix there.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.offline_pipelines import OfflinePipelineCreate, OfflinePipelineRead, OfflinePipelineUpdate
from src.backend.services.offline_pipeline_service import (
    OfflinePipelineConflictError,
    OfflinePipelineInvalidError,
    OfflinePipelineNotFoundError,
    OfflinePipelineService,
)
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, require_any_permission, require_permission

router = APIRouter(prefix="/gateway/offline-pipelines", tags=["offline-pipelines"])
ReadUser = Annotated[CurrentUser, Depends(require_any_permission("applications.read_own", "applications.read_global"))]
ManageUser = Annotated[CurrentUser, Depends(require_permission("applications.manage_own"))]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def _can_pick_models(user: CurrentUser) -> None:
    # A pipeline is built from the user's own endpoints, so it also needs the right to manage their pools.
    if "model_pools.manage_own" not in user.permissions:
        raise HTTPException(status_code=403, detail="Managing offline pipelines requires model_pools.manage_own")


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, OfflinePipelineNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, OfflinePipelineConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


_ERRORS = (OfflinePipelineNotFoundError, OfflinePipelineConflictError, OfflinePipelineInvalidError)


@router.get("", response_model=list[OfflinePipelineRead])
async def list_offline_pipelines(user: ReadUser, session: DbSession) -> list[OfflinePipelineRead]:
    return await OfflinePipelineService(session).list(user.id)


@router.post("", response_model=OfflinePipelineRead, status_code=status.HTTP_201_CREATED)
async def create_offline_pipeline(
    payload: OfflinePipelineCreate, user: ManageUser, session: DbSession
) -> OfflinePipelineRead:
    _can_pick_models(user)
    try:
        return await OfflinePipelineService(session).create(user.id, payload)
    except _ERRORS as exc:
        raise _error(exc) from exc


@router.get("/{pipeline_id}", response_model=OfflinePipelineRead)
async def get_offline_pipeline(pipeline_id: str, user: ReadUser, session: DbSession) -> OfflinePipelineRead:
    try:
        return await OfflinePipelineService(session).get(user.id, pipeline_id)
    except _ERRORS as exc:
        raise _error(exc) from exc


@router.patch("/{pipeline_id}", response_model=OfflinePipelineRead)
async def update_offline_pipeline(
    pipeline_id: str, payload: OfflinePipelineUpdate, user: ManageUser, session: DbSession
) -> OfflinePipelineRead:
    _can_pick_models(user)
    try:
        return await OfflinePipelineService(session).update(user.id, pipeline_id, payload)
    except _ERRORS as exc:
        raise _error(exc) from exc


@router.delete("/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_offline_pipeline(pipeline_id: str, user: ManageUser, session: DbSession) -> Response:
    try:
        await OfflinePipelineService(session).delete(user.id, pipeline_id)
    except _ERRORS as exc:
        raise _error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
