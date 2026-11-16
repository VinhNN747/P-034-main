from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.dtos.gateway_runtime import GatewayRuntimeInput
from src.backend_ai.gateway.factory import (
    GatewayConfigurationError,
    build_orchestrator,
    build_pipeline,
    ensure_strategy_available,
)
from src.backend_ai.services.gateway_execution_service import (
    ApplicationDatabaseRouter as ApplicationDatabaseRouter,
)
from src.backend_ai.services.gateway_execution_service import (
    GatewayExecutionService,
)
from src.backend_ai.services.internal_provider_clients import build_internal_clients
from src.shared.config import get_settings
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.core import Application
from src.shared.models.gateway import ApplicationGatewayConfig
from src.shared.services.credential_crypto import CredentialConfigurationError, ensure_credential_crypto
from src.shared.services.gateway_runtime_service import GatewayRuntimeService
from src.shared.services.provider_credential_resolver import ProviderCredentialResolver

router = APIRouter(prefix="/gateway", tags=["gateway-runtime"])
AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]




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


def _validate_strategies(payload: GatewayRuntimeInput) -> None:
    allowed = {
        "understander": {"rule", "llm", "jev", "classifier"},
        "decomposer": {"none", "deterministic", "llm"},
        "router": {"deterministic", "database", "jev"},
        "aggregator": {"auto", "none", "deterministic", "llm"},
    }
    for field, choices in allowed.items():
        value = getattr(payload, field)
        if value is None and field == "understander":
            continue  # filled from the application's gateway config
        if value not in choices:
            raise HTTPException(status_code=400, detail=f"Unknown {field} '{value}'")


async def _run(payload: GatewayRuntimeInput, user: CurrentUser, session: AsyncSession, latency_runtime=None) -> dict:
    return await GatewayExecutionService(session, get_settings(), latency_runtime=latency_runtime).run_legacy(
        payload,
        user,
        orchestrator_builder=build_orchestrator,
    )


@router.post("/analyze")
async def analyze(payload: GatewayRuntimeInput, user: AuthUser, session: DbSession) -> dict:
    _validate_strategies(payload)
    settings = get_settings()
    await _owned_application(session, user.id, payload.application_id)
    if payload.understander is None:
        config = await session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == payload.application_id)
        )
        payload = payload.model_copy(update={"understander": config.default_understander if config else "rule"})
    request = payload.to_gateway_request()
    pricing, _ = await GatewayRuntimeService(session).model_runtime_catalog(user.id)
    try:
        crypto = ensure_credential_crypto(settings)
        internal_clients = await build_internal_clients(
            ProviderCredentialResolver(session, crypto),
            settings,
            need_llm=payload.understander == "llm" or payload.decomposer == "llm",
            need_jev=payload.understander == "jev",
            need_ocr=request.has_attachments(),
        )
        for strategy in (payload.understander, payload.decomposer):
            if strategy in {"llm", "jev"}:
                ensure_strategy_available(strategy, settings, internal_clients)
        if session.in_transaction():
            await session.commit()
        result = await build_pipeline(
            settings=settings,
            pricing=pricing,
            internal_clients=internal_clients,
        ).run(
            request,
            understander=payload.understander,
            decomposer=payload.decomposer,
        )
    except (GatewayConfigurationError, CredentialConfigurationError) as exc:
        raise HTTPException(status_code=503, detail="The requested gateway strategy is unavailable.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = result.model_dump(mode="json")
    body["request_id"] = request.request_id
    return body


@router.post("/requests")
async def create_request(request: Request, payload: GatewayRuntimeInput, user: AuthUser, session: DbSession) -> dict:
    return await _run(payload, user, session, getattr(request.app.state, "latency_runtime", None))
