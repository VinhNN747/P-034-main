from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.application_keys import (
    ApplicationKeyCreate,
    ApplicationKeyCreated,
    ApplicationKeyPage,
    ApplicationKeyRead,
)
from src.backend.services.application_key_service import ApplicationKeyNotFoundError, ApplicationKeyService
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, require_any_permission

router = APIRouter(prefix="/applications", tags=["application-api-keys"])
ReadUser = Annotated[
    CurrentUser,
    Depends(require_any_permission("applications.read_own", "applications.write_own", "applications.manage_own")),
]
ManageUser = Annotated[
    CurrentUser, Depends(require_any_permission("applications.write_own", "applications.manage_own"))
]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("/{application_id}/api-keys", response_model=ApplicationKeyPage)
async def list_application_keys(
    application_id: str,
    user: ReadUser,
    session: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ApplicationKeyPage:
    try:
        return await ApplicationKeyService(session).list(user.id, application_id, page=page, page_size=page_size)
    except ApplicationKeyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{application_id}/api-keys", response_model=ApplicationKeyCreated, status_code=201)
async def create_application_key(
    application_id: str,
    payload: ApplicationKeyCreate,
    user: ManageUser,
    session: DbSession,
) -> ApplicationKeyCreated:
    try:
        return await ApplicationKeyService(session).create(user.id, application_id, payload)
    except ApplicationKeyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{application_id}/api-keys/{key_id}/revoke", response_model=ApplicationKeyRead)
async def revoke_application_key(
    application_id: str,
    key_id: str,
    user: ManageUser,
    session: DbSession,
) -> ApplicationKeyRead:
    try:
        return await ApplicationKeyService(session).revoke(user.id, application_id, key_id)
    except ApplicationKeyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
