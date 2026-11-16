from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.dependencies.ai_service_auth import require_ai_service_token
from src.backend_ai.services.internal_provider_clients import build_internal_clients
from src.backend_ai.services.model_probe_execution_service import ModelProbeExecutionService
from src.backend_ai.services.provider_connection_probe_service import (
    ProviderConnectionProbeService,
    ProviderProbeConflictError,
    ProviderProbeInvalidError,
    ProviderProbeNotFoundError,
)
from src.backend_ai.services.support_response_synthesizer import SupportResponseSynthesizer
from src.backend_ai.services.support_understanding_service import SupportUnderstandingService
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.dtos.ai_internal import (
    SupportSynthesisInput,
    SupportSynthesisOutput,
    SupportUnderstandingInput,
    SupportUnderstandingOutput,
)
from src.shared.dtos.model_catalog import ModelProbeRead
from src.shared.dtos.provider_connection_probe import ConnectionProbeInput, ConnectionProbeRead
from src.shared.services.credential_crypto import CredentialConfigurationError, ensure_credential_crypto
from src.shared.services.provider_credential_resolver import ProviderCredentialResolver

router = APIRouter(prefix="/internal/v1", tags=["internal-ai"], dependencies=[Depends(require_ai_service_token)])
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/provider-connections/{provider_id}/probe", response_model=ConnectionProbeRead)
async def probe_provider_connection(
    provider_id: str,
    payload: ConnectionProbeInput,
    session: DbSession,
    actor_user_id: Annotated[str | None, Header(alias="X-Actor-User-ID")] = None,
) -> ConnectionProbeRead:
    settings = get_settings()
    try:
        crypto = ensure_credential_crypto(settings)
    except CredentialConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Credential encryption is unavailable.") from exc
    service = ProviderConnectionProbeService(
        session,
        ProviderCredentialResolver(session, crypto),
        settings,
        actor_user_id=actor_user_id,
    )
    try:
        return await service.probe(provider_id, payload)
    except ProviderProbeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderProbeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderProbeInvalidError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/support/understand", response_model=SupportUnderstandingOutput)
async def understand_support(payload: SupportUnderstandingInput, session: DbSession) -> SupportUnderstandingOutput:
    settings = get_settings()
    try:
        internal_clients = await build_internal_clients(
            ProviderCredentialResolver(session, ensure_credential_crypto(settings)),
            settings,
            need_support=True,
        )
    except CredentialConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Internal provider credentials are unavailable.") from exc
    if session.in_transaction():
        await session.commit()
    return await SupportUnderstandingService(settings, internal_clients.support_client).understand(payload)


@router.post("/support/synthesize", response_model=SupportSynthesisOutput)
async def synthesize(payload: SupportSynthesisInput, session: DbSession) -> SupportSynthesisOutput:
    settings = get_settings()
    try:
        internal_clients = await build_internal_clients(
            ProviderCredentialResolver(session, ensure_credential_crypto(settings)),
            settings,
            need_support=True,
        )
    except CredentialConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Internal provider credentials are unavailable.") from exc
    if session.in_transaction():
        await session.commit()
    message = await SupportResponseSynthesizer.from_settings(settings, client=internal_clients.support_client).synthesize(
        question=payload.question, response=payload.response, history=payload.history
    )
    return SupportSynthesisOutput(message=message)


@router.post("/models/{model_id}/probe", response_model=ModelProbeRead)
async def probe_model(model_id: str, session: DbSession) -> ModelProbeRead:
    try:
        return await ModelProbeExecutionService(session, get_settings()).probe(model_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
