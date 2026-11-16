from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.clients.ai_backend_client import AIBackendClient, AIBackendUnavailableError, get_ai_backend_client
from src.backend.dtos.catalog import (
    FallbackSuggestion,
    FallbackSuggestionRequest,
    ModelCreate,
    ModelPoolCreate,
    ModelPoolRead,
    ModelPoolUpdate,
    ModelUpdate,
    PolicyCreate,
    PolicyRead,
    ProviderCreate,
    ProviderRead,
)
from src.backend.services.catalog_service import (
    CatalogConflictError,
    CatalogReferenceError,
    CatalogService,
)
from src.backend.services.model_catalog_service import ModelCatalogService
from src.backend.services.model_probe_service import ModelProbeService
from src.shared.database.session import get_db
from src.shared.dependencies.auth import (
    CurrentUser,
    get_current_user,
    require_permission,
)
from src.shared.dtos.model_catalog import ModelCatalogPage, ModelCatalogRead, ModelProbeRead

router = APIRouter(prefix="/gateway", tags=["gateway-catalog"])
AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
AvailableModelReadUser = Annotated[CurrentUser, Depends(require_permission("models.read_available"))]
AdminModelReadUser = Annotated[CurrentUser, Depends(require_permission("models.read"))]
AdminModelManageUser = Annotated[CurrentUser, Depends(require_permission("models.manage"))]
ModelPoolReadUser = Annotated[CurrentUser, Depends(require_permission("model_pools.read_own"))]
ModelPoolManageUser = Annotated[CurrentUser, Depends(require_permission("model_pools.manage_own"))]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def catalog_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CatalogConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/providers", response_model=list[ProviderRead])
async def list_providers(_: AuthUser, session: DbSession) -> list[ProviderRead]:
    return await CatalogService(session).list_providers()


@router.post("/providers", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def create_provider(payload: ProviderCreate, user: AdminModelManageUser, session: DbSession) -> ProviderRead:
    try:
        return await CatalogService(session).create_provider(payload, user.id)
    except (CatalogConflictError, CatalogReferenceError) as exc:
        raise catalog_error(exc) from exc


@router.get("/available-models", response_model=ModelCatalogPage)
async def list_available_models(
    user: AvailableModelReadUser,
    session: DbSession,
    provider: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    search: str | None = Query(default=None, max_length=150),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort_by: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None),
) -> ModelCatalogPage:
    return await ModelCatalogService(session).list(
        provider=provider,
        status=status_filter,
        search=search,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
        available_only=True,
        viewer_user_id=user.id,
    )


@router.get("/models", response_model=ModelCatalogPage)
async def list_models(
    _: AdminModelReadUser,
    session: DbSession,
    provider: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    search: str | None = Query(default=None, max_length=150),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort_by: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None),
) -> ModelCatalogPage:
    return await ModelCatalogService(session).list(
        provider=provider,
        status=status_filter,
        search=search,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@router.post("/models", response_model=ModelCatalogRead, status_code=status.HTTP_201_CREATED)
async def create_model(payload: ModelCreate, _: AdminModelManageUser, session: DbSession) -> ModelCatalogRead:
    try:
        return await ModelCatalogService(session).create(payload)
    except (CatalogConflictError, CatalogReferenceError) as exc:
        raise catalog_error(exc) from exc


@router.patch("/models/{model_id}", response_model=ModelCatalogRead)
async def update_model(
    model_id: str, payload: ModelUpdate, _: AdminModelManageUser, session: DbSession
) -> ModelCatalogRead:
    try:
        return await ModelCatalogService(session).update(model_id, payload)
    except (CatalogConflictError, CatalogReferenceError) as exc:
        raise catalog_error(exc) from exc


@router.post("/models/{model_id}/probe", response_model=ModelProbeRead)
async def probe_model(
    model_id: str,
    _: AdminModelManageUser,
    session: DbSession,
    ai_client: Annotated[AIBackendClient, Depends(get_ai_backend_client)],
) -> ModelProbeRead:
    try:
        return await ModelProbeService(session, ai_client).probe(model_id)
    except CatalogReferenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIBackendUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/model-pools", response_model=list[ModelPoolRead])
async def list_model_pools(user: ModelPoolReadUser, session: DbSession) -> list[ModelPoolRead]:
    return await CatalogService(session).list_pools(user.id)


@router.post("/model-pools", response_model=ModelPoolRead, status_code=status.HTTP_201_CREATED)
async def create_model_pool(payload: ModelPoolCreate, user: ModelPoolManageUser, session: DbSession) -> ModelPoolRead:
    try:
        return await CatalogService(session).create_pool(payload, user.id, enforce_tier_rule=True)
    except (CatalogConflictError, CatalogReferenceError) as exc:
        raise catalog_error(exc) from exc


@router.post("/model-pools/fallback-suggestions", response_model=list[FallbackSuggestion])
async def suggest_pool_fallbacks(
    payload: FallbackSuggestionRequest,
    user: ModelPoolManageUser,
    session: DbSession,
) -> list[FallbackSuggestion]:
    """System-picked fallbacks for the chosen primaries, shown to the user before saving."""
    try:
        return await CatalogService(session).suggest_fallbacks(payload, user.id)
    except CatalogReferenceError as exc:
        raise catalog_error(exc) from exc


@router.get("/model-pools/{pool_id}", response_model=ModelPoolRead)
async def get_model_pool(pool_id: str, user: ModelPoolReadUser, session: DbSession) -> ModelPoolRead:
    try:
        return await CatalogService(session).get_pool(pool_id, user.id)
    except CatalogReferenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/model-pools/{pool_id}", response_model=ModelPoolRead)
async def update_model_pool(
    pool_id: str,
    payload: ModelPoolUpdate,
    user: ModelPoolManageUser,
    session: DbSession,
) -> ModelPoolRead:
    try:
        return await CatalogService(session).update_pool(pool_id, payload, user.id, enforce_tier_rule=True)
    except CatalogReferenceError as exc:
        raise HTTPException(status_code=404 if str(exc) == "Model pool not found" else 422, detail=str(exc)) from exc
    except CatalogConflictError as exc:
        raise catalog_error(exc) from exc


@router.delete("/model-pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_pool(pool_id: str, user: ModelPoolManageUser, session: DbSession) -> Response:
    try:
        await CatalogService(session).delete_pool(pool_id, user.id)
    except CatalogReferenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CatalogConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/routing-policies", response_model=list[PolicyRead])
async def list_routing_policies(_: AuthUser, session: DbSession) -> list[PolicyRead]:
    return await CatalogService(session).list_policies()


@router.post("/routing-policies", response_model=PolicyRead, status_code=status.HTTP_201_CREATED)
async def create_routing_policy(payload: PolicyCreate, _: AuthUser, session: DbSession) -> PolicyRead:
    try:
        return await CatalogService(session).create_policy(payload)
    except (CatalogConflictError, CatalogReferenceError) as exc:
        raise catalog_error(exc) from exc


@router.get("/routing-policies/{policy_id}", response_model=PolicyRead)
async def get_routing_policy(policy_id: str, _: AuthUser, session: DbSession) -> PolicyRead:
    try:
        return await CatalogService(session).get_policy(policy_id)
    except CatalogReferenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
