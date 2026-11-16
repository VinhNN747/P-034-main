"""A user's own OpenAI-compatible model servers (bring your own endpoint)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.clients.ai_backend_client import AIBackendUnavailableError
from src.backend.dtos.customer_endpoints import EndpointCreate, EndpointModelCreate, EndpointRead
from src.backend.services.customer_endpoint_service import (
    CustomerEndpointService,
    EndpointConflictError,
    EndpointInvalidError,
    EndpointNotFoundError,
)
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, require_permission
from src.shared.services.credential_crypto import (
    CredentialConfigurationError,
    CredentialCrypto,
    ensure_credential_crypto,
)

router = APIRouter(prefix="/gateway/my-endpoints", tags=["customer-endpoints"])
# Same claims as the user's own pools: an endpoint only matters through a pool.
ReadUser = Annotated[CurrentUser, Depends(require_permission("model_pools.read_own"))]
ManageUser = Annotated[CurrentUser, Depends(require_permission("model_pools.manage_own"))]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def _crypto() -> CredentialCrypto | None:
    # An endpoint without an API key needs no encryption key, so a missing one is not an error here.
    try:
        return ensure_credential_crypto(get_settings())
    except CredentialConfigurationError:
        return None


Crypto = Annotated[CredentialCrypto | None, Depends(_crypto)]


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, EndpointNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, EndpointConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


_SERVICE_ERRORS = (EndpointNotFoundError, EndpointConflictError, EndpointInvalidError)


@router.get("", response_model=list[EndpointRead])
async def list_endpoints(user: ReadUser, session: DbSession, crypto: Crypto) -> list[EndpointRead]:
    return await CustomerEndpointService(session, crypto).list(user.id)


@router.post("", response_model=EndpointRead, status_code=status.HTTP_201_CREATED)
async def create_endpoint(payload: EndpointCreate, user: ManageUser, session: DbSession, crypto: Crypto) -> EndpointRead:
    try:
        return await CustomerEndpointService(session, crypto).create(user.id, payload)
    except _SERVICE_ERRORS as exc:
        raise _error(exc) from exc


@router.get("/{endpoint_id}", response_model=EndpointRead)
async def get_endpoint(endpoint_id: str, user: ReadUser, session: DbSession, crypto: Crypto) -> EndpointRead:
    try:
        return await CustomerEndpointService(session, crypto).get(user.id, endpoint_id)
    except _SERVICE_ERRORS as exc:
        raise _error(exc) from exc


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(endpoint_id: str, user: ManageUser, session: DbSession, crypto: Crypto) -> Response:
    try:
        await CustomerEndpointService(session, crypto).delete(user.id, endpoint_id)
    except _SERVICE_ERRORS as exc:
        raise _error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{endpoint_id}/discover", response_model=list[str])
async def discover_models(endpoint_id: str, user: ManageUser, session: DbSession, crypto: Crypto) -> list[str]:
    """Model names the server lists (GET {base_url}/models), so they can be picked instead of typed."""
    try:
        return await CustomerEndpointService(session, crypto).discover_models(user.id, endpoint_id)
    except _SERVICE_ERRORS as exc:
        raise _error(exc) from exc


@router.post("/{endpoint_id}/models", response_model=EndpointRead, status_code=status.HTTP_201_CREATED)
async def add_model(
    endpoint_id: str, payload: EndpointModelCreate, user: ManageUser, session: DbSession, crypto: Crypto
) -> EndpointRead:
    try:
        return await CustomerEndpointService(session, crypto).add_model(user.id, endpoint_id, payload)
    except _SERVICE_ERRORS as exc:
        raise _error(exc) from exc


@router.post("/{endpoint_id}/models/{model_id}/probe", response_model=EndpointRead)
async def probe_model(
    endpoint_id: str, model_id: str, user: ManageUser, session: DbSession, crypto: Crypto
) -> EndpointRead:
    try:
        return await CustomerEndpointService(session, crypto).probe_model(user.id, endpoint_id, model_id)
    except _SERVICE_ERRORS as exc:
        raise _error(exc) from exc
    except AIBackendUnavailableError:
        raise HTTPException(status_code=503, detail="AI service is unavailable.") from None


@router.delete("/{endpoint_id}/models/{model_id}", response_model=EndpointRead)
async def delete_model(
    endpoint_id: str, model_id: str, user: ManageUser, session: DbSession, crypto: Crypto
) -> EndpointRead:
    try:
        return await CustomerEndpointService(session, crypto).delete_model(user.id, endpoint_id, model_id)
    except _SERVICE_ERRORS as exc:
        raise _error(exc) from exc
