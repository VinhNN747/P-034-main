"""Secret-safe DB credential access for opt-in developer/evaluation scripts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.shared.config import get_settings
from src.shared.database.session import create_engine_from_settings
from src.shared.models.gateway import Provider
from src.shared.services.credential_crypto import ensure_credential_crypto
from src.shared.services.provider_credential_resolver import (
    CredentialUnavailableError,
    ProviderCredentialResolver,
)


async def resolve_service_key(purpose: str) -> str:
    """Resolve an internal_openai or routing_jev service binding from the DB."""

    settings = get_settings()
    crypto = ensure_credential_crypto(settings)
    engine = create_engine_from_settings(settings=settings)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            credential = await ProviderCredentialResolver(session, crypto).resolve_service(purpose)
            return credential.api_key
    except CredentialUnavailableError:
        raise RuntimeError(f"The {purpose} provider credential is unavailable.") from None
    finally:
        await engine.dispose()


async def resolve_target_key(kind: str) -> str:
    """Resolve the first active target connection of ``kind`` for eval tools."""

    settings = get_settings()
    crypto = ensure_credential_crypto(settings)
    engine = create_engine_from_settings(settings=settings)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            resolver = ProviderCredentialResolver(session, crypto)
            provider_ids = await session.scalars(
                select(Provider.id)
                .where(Provider.kind == kind, Provider.purpose == "target", Provider.status == "active")
                .order_by(Provider.name, Provider.id)
            )
            for provider_id in provider_ids:
                try:
                    return (await resolver.resolve_provider(provider_id)).api_key
                except CredentialUnavailableError:
                    continue
    finally:
        await engine.dispose()
    raise RuntimeError(f"No configured {kind} target credential is available.")
