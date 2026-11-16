from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.clients.ai_backend_client import AIBackendUnavailableError, get_ai_backend_client
from src.backend.dtos.provider_credentials import (
    BindingPurpose,
    ProviderConnectionCreate,
    ProviderConnectionRead,
    ProviderConnectionUpdate,
    ProviderCredentialReplace,
    ServiceProviderBindingRead,
    ServiceProviderBindingUpdate,
)
from src.backend.services.provider_connection_service import (
    ProviderConnectionConflictError,
    ProviderConnectionInvalidError,
    ProviderConnectionNotFoundError,
    ProviderConnectionService,
)
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, require_permission
from src.shared.dtos.provider_connection_probe import ConnectionProbeInput, ConnectionProbeRead
from src.shared.services.credential_crypto import (
    CredentialConfigurationError,
    CredentialCrypto,
    ensure_credential_crypto,
)

router = APIRouter(prefix="/gateway", tags=["provider-connections"])
ManageUser = Annotated[CurrentUser, Depends(require_permission("models.manage"))]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_credential_crypto() -> CredentialCrypto | None:
    try:
        return ensure_credential_crypto(get_settings())
    except CredentialConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credential encryption is not available.",
        ) from None


CredentialCryptoDependency = Annotated[CredentialCrypto | None, Depends(get_credential_crypto)]


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProviderConnectionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ProviderConnectionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CredentialConfigurationError):
        return HTTPException(status_code=503, detail="Credential encryption is not available.")
    if isinstance(exc, ProviderConnectionInvalidError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="Provider connection request failed.")


@router.post("/provider-connections/{provider_id}/probe", response_model=ConnectionProbeRead)
async def probe_connection(
    provider_id: str,
    payload: ConnectionProbeInput,
    user: ManageUser,
    session: DbSession,
) -> ConnectionProbeRead:
    try:
        return await ProviderConnectionService(session, None).probe_connection(
            provider_id,
            payload,
            user.id,
            ai_client=get_ai_backend_client(),
        )
    except AIBackendUnavailableError as exc:
        forwarded_status = exc.status_code if exc.status_code in {404, 409, 422} else 503
        raise HTTPException(
            status_code=forwarded_status,
            detail=(
                "Provider connection not found."
                if forwarded_status == 404
                else "Credential changed; reload connection status and retry."
                if forwarded_status == 409
                else "The requested provider test is invalid."
                if forwarded_status == 422
                else "AI service is unavailable."
            ),
        ) from None


@router.get("/provider-connections", response_model=list[ProviderConnectionRead])
async def list_connections(
    _: ManageUser,
    session: DbSession,
    crypto: CredentialCryptoDependency,
) -> list[ProviderConnectionRead]:
    return await ProviderConnectionService(session, crypto).list_connections()


@router.post(
    "/provider-connections",
    response_model=ProviderConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    payload: ProviderConnectionCreate,
    user: ManageUser,
    session: DbSession,
    crypto: CredentialCryptoDependency,
) -> ProviderConnectionRead:
    try:
        return await ProviderConnectionService(session, crypto).create(payload, user.id)
    except (ProviderConnectionNotFoundError, ProviderConnectionConflictError,
            ProviderConnectionInvalidError, CredentialConfigurationError) as exc:
        raise _service_error(exc) from exc


@router.patch("/provider-connections/{provider_id}", response_model=ProviderConnectionRead)
async def update_connection(
    provider_id: str,
    payload: ProviderConnectionUpdate,
    user: ManageUser,
    session: DbSession,
    crypto: CredentialCryptoDependency,
) -> ProviderConnectionRead:
    try:
        return await ProviderConnectionService(session, crypto).update(provider_id, payload, user.id)
    except (ProviderConnectionNotFoundError, ProviderConnectionConflictError,
            ProviderConnectionInvalidError) as exc:
        raise _service_error(exc) from exc


@router.put("/provider-connections/{provider_id}/credential", response_model=ProviderConnectionRead)
async def replace_credential(
    provider_id: str,
    payload: ProviderCredentialReplace,
    user: ManageUser,
    session: DbSession,
    crypto: CredentialCryptoDependency,
) -> ProviderConnectionRead:
    try:
        return await ProviderConnectionService(session, crypto).replace_credential(provider_id, payload, user.id)
    except (ProviderConnectionNotFoundError, ProviderConnectionConflictError,
            ProviderConnectionInvalidError, CredentialConfigurationError) as exc:
        raise _service_error(exc) from exc


@router.delete("/provider-connections/{provider_id}/credential", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_credential(
    provider_id: str,
    user: ManageUser,
    session: DbSession,
    crypto: CredentialCryptoDependency,
    expected_version: int = Query(ge=1),
) -> Response:
    try:
        await ProviderConnectionService(session, crypto).revoke_credential(provider_id, expected_version, user.id)
    except (ProviderConnectionNotFoundError, ProviderConnectionConflictError,
            ProviderConnectionInvalidError, CredentialConfigurationError) as exc:
        raise _service_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/service-provider-bindings", response_model=list[ServiceProviderBindingRead])
async def list_bindings(
    _: ManageUser,
    session: DbSession,
    crypto: CredentialCryptoDependency,
) -> list[ServiceProviderBindingRead]:
    return await ProviderConnectionService(session, crypto).list_bindings()


@router.put("/service-provider-bindings/{purpose}", response_model=list[ServiceProviderBindingRead])
async def set_binding(
    purpose: BindingPurpose,
    payload: ServiceProviderBindingUpdate,
    user: ManageUser,
    session: DbSession,
    crypto: CredentialCryptoDependency,
) -> list[ServiceProviderBindingRead]:
    try:
        return await ProviderConnectionService(session, crypto).set_binding(
            purpose, payload.provider_id, user.id
        )
    except (ProviderConnectionNotFoundError, ProviderConnectionConflictError,
            ProviderConnectionInvalidError) as exc:
        raise _service_error(exc) from exc
