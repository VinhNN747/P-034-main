from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.gateway_configuration import GatewayConfigRead, GatewayConfigUpsert
from src.backend.services.catalog_service import CatalogReferenceError
from src.backend.services.gateway_configuration_service import GatewayConfigConflictError, GatewayConfigurationService
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user

router = APIRouter(tags=["gateway-configuration"])
AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get(
    "/applications/{application_id}/gateway-config",
    response_model=GatewayConfigRead,
)
async def get_gateway_config(application_id: str, current_user: AuthUser, session: DbSession) -> GatewayConfigRead:
    try:
        return await GatewayConfigurationService(session).get(current_user.id, application_id)
    except CatalogReferenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/applications/{application_id}/gateway-config",
    response_model=GatewayConfigRead,
)
async def upsert_gateway_config(
    application_id: str,
    payload: GatewayConfigUpsert,
    current_user: AuthUser,
    session: DbSession,
) -> GatewayConfigRead:
    try:
        return await GatewayConfigurationService(session).upsert(current_user.id, application_id, payload)
    except GatewayConfigConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CatalogReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
