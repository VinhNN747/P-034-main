# The CLI supports `python src/scripts/seed_backend.py` from the repository root.
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.backend.dtos.applications import ApplicationCreate
from src.backend.dtos.catalog import (
    ModelCreate,
    PolicyCreate,
    ProviderCreate,
)
from src.backend.dtos.gateway_configuration import GatewayConfigUpsert
from src.backend.services.application_service import ApplicationService
from src.backend.services.catalog_service import CatalogService
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.shared.database.session import create_engine_from_settings
from src.shared.models.core import Application
from src.shared.models.gateway import (
    ApplicationGatewayConfig,
    Model,
    ModelPool,
    Provider,
    RoutingPolicy,
)

SEED_OWNER_ID = "seed-owner"


@dataclass(frozen=True)
class SeedSummary:
    providers: int
    models: int
    pools: int
    policies: int
    applications: int
    configurations: int


@dataclass(frozen=True)
class SeedModelSpec:
    provider: str
    name: str
    display_name: str
    capabilities: tuple[str, ...]
    quality_tier: str
    quality_score: float
    context_window: int
    target_latency_ms: int
    input_price_per_million: float
    output_price_per_million: float
    pricing_version: str


# Bootstrap data only. Runtime routing and pricing read the normalized database
# tables and do not depend on this script or a filesystem model catalog.
SEED_MODEL_SPECS = (
    SeedModelSpec(
        provider="openai",
        name="gpt-4o-mini",
        display_name="GPT-4o mini",
        capabilities=("text", "chat", "structured_output", "summarization", "classification"),
        quality_tier="economy",
        quality_score=1,
        context_window=128_000,
        target_latency_ms=600,
        input_price_per_million=0.15,
        output_price_per_million=0.60,
        pricing_version="openai-docs-2026-09",
    ),
    SeedModelSpec(
        provider="openai",
        name="gpt-5.6-luna",
        display_name="GPT-5.6 Luna",
        capabilities=(
            "text",
            "chat",
            "structured_output",
            "summarization",
            "classification",
            "code_generation",
            "reasoning",
            "data_analysis",
            "long_context",
        ),
        quality_tier="economy",
        quality_score=1,
        context_window=1_050_000,
        target_latency_ms=900,
        input_price_per_million=0.20,
        output_price_per_million=1.20,
        pricing_version="openai-docs-2026-09",
    ),
    SeedModelSpec(
        provider="google",
        name="gemini-3.6-flash",
        display_name="Gemini 3.6 Flash",
        capabilities=(
            "text",
            "chat",
            "structured_output",
            "summarization",
            "classification",
            "code_generation",
            "reasoning",
            "data_analysis",
            "long_context",
        ),
        quality_tier="balanced",
        quality_score=2,
        context_window=1_048_576,
        target_latency_ms=800,
        input_price_per_million=0.75,
        output_price_per_million=3.75,
        pricing_version="google-gemini-api-standard-2026",
    ),
    SeedModelSpec(
        provider="openai",
        name="gpt-5.6-terra",
        display_name="GPT-5.6 Terra",
        capabilities=(
            "text",
            "chat",
            "structured_output",
            "summarization",
            "classification",
            "code_generation",
            "reasoning",
            "data_analysis",
            "long_context",
        ),
        quality_tier="premium",
        quality_score=3,
        context_window=1_050_000,
        target_latency_ms=1_400,
        input_price_per_million=2.00,
        output_price_per_million=12.00,
        pricing_version="openai-docs-2026-09",
    ),
)


async def seed_session(session: AsyncSession, owner_id: str = SEED_OWNER_ID) -> SeedSummary:
    catalog = CatalogService(session)
    configured_models = SEED_MODEL_SPECS
    providers_by_name: dict[str, Provider] = {}
    provider_metadata = {
        "openai": "OpenAI",
        "google": "Google Gemini",
    }
    configured_providers = list(dict.fromkeys(model.provider for model in configured_models))
    provider_payloads = [ProviderCreate(name="offline", display_name="Offline", kind="offline")]
    provider_payloads.extend(
        ProviderCreate(
            name=provider_name,
            display_name=provider_metadata[provider_name],
            kind=provider_name,
        )
        for provider_name in configured_providers
    )
    for payload in provider_payloads:
        known_model = next(
            (model.name for model in configured_models if model.provider == payload.kind),
            None,
        )
        provider = None
        if known_model:
            provider = await session.scalar(
                select(Provider)
                .join(Model, Model.provider_id == Provider.id)
                .where(
                    Provider.kind == payload.kind,
                    Provider.status == "active",
                    Model.name == known_model,
                )
                .order_by(Provider.created_at)
            )
        if provider is None:
            provider = await session.scalar(select(Provider).where(Provider.name == payload.name))
        if provider is None:
            provider = await session.scalar(
                select(Provider)
                .where(Provider.kind == payload.kind, Provider.status == "active")
                .order_by(Provider.created_at)
            )
        if provider is None:
            provider = Provider(**payload.model_dump())
            session.add(provider)
            await session.commit()
            await session.refresh(provider)
        providers_by_name[payload.name] = provider

    local_model_specs = (
        ("local-deterministic", "Local deterministic", "offline", ["chat", "classification", "summarization"], 50, 0.0, 0.0, "local-v1"),
        ("local-fast", "Local fast", "offline", ["chat", "classification"], 25, 0.0, 0.0, "local-v1"),
    )
    models_by_name: dict[str, Model] = {}
    for name, display_name, provider_name, capabilities, latency, input_price, output_price, pricing_version in local_model_specs:
        provider = providers_by_name[provider_name]
        model = await session.scalar(select(Model).where(Model.provider_id == provider.id, Model.name == name))
        if model is None:
            model_read = await catalog.create_model(
                ModelCreate(
                    provider_id=provider.id,
                    name=name,
                    display_name=display_name,
                    capabilities=capabilities,
                    target_latency_ms=latency,
                    input_price_per_million=input_price,
                    output_price_per_million=output_price,
                    pricing_version=pricing_version,
                    quality_tier="economy",
                )
            )
            model = await session.get(Model, model_read.id)
        else:
            model.display_name = display_name
            model.capabilities_json = capabilities
            model.target_latency_ms = latency
            model.input_price_per_million = input_price
            model.output_price_per_million = output_price
            model.pricing_version = pricing_version
            model.quality_tier = "economy"
        models_by_name[name] = model

    for spec in configured_models:
        provider = providers_by_name[spec.provider]
        model = await session.scalar(
            select(Model).where(Model.provider_id == provider.id, Model.name == spec.name)
        )
        if model is None:
            model_read = await catalog.create_model(
                ModelCreate(
                    provider_id=provider.id,
                    name=spec.name,
                    display_name=spec.display_name,
                    capabilities=list(spec.capabilities),
                    context_window=spec.context_window,
                    target_latency_ms=spec.target_latency_ms,
                    input_price_per_million=spec.input_price_per_million,
                    output_price_per_million=spec.output_price_per_million,
                    pricing_version=spec.pricing_version,
                    quality_tier=spec.quality_tier,
                )
            )
            model = await session.get(Model, model_read.id)
        else:
            model.display_name = spec.display_name
            model.capabilities_json = list(spec.capabilities)
            model.context_window = spec.context_window
            model.target_latency_ms = spec.target_latency_ms
            model.input_price_per_million = spec.input_price_per_million
            model.output_price_per_million = spec.output_price_per_million
            model.pricing_version = spec.pricing_version
            model.quality_tier = spec.quality_tier
        models_by_name[spec.name] = model

    coding_names = [
        item.name for item in configured_models if "code_generation" in item.capabilities
    ] or [item.name for item in configured_models]
    reasoning_names = [
        item.name for item in configured_models if "reasoning" in item.capabilities
    ] or [item.name for item in configured_models]

    policies_by_name: dict[str, RoutingPolicy] = {}
    configured_by_cost = sorted(
        configured_models,
        key=lambda item: item.input_price_per_million + item.output_price_per_million,
    )
    quality_scores = {
        models_by_name[item.name].id: item.quality_score for item in configured_models
    }
    for name, fallback_names in (
        ("general", [item.name for item in configured_by_cost]),
        ("coding", coding_names),
        ("reasoning", reasoning_names),
    ):
        fallback = [models_by_name[model_name].id for model_name in fallback_names]
        policy = await session.scalar(select(RoutingPolicy).where(RoutingPolicy.name == name))
        if policy is None:
            policy_read = await catalog.create_policy(
                PolicyCreate(
                    name=name,
                    guardrails={"quality_scores": quality_scores},
                    fallback_chain=fallback,
                )
            )
            policy = await session.get(RoutingPolicy, policy_read.id)
        else:
            guardrails = dict(policy.guardrails_json or {})
            guardrails["quality_scores"] = {
                **guardrails.get("quality_scores", {}),
                **quality_scores,
            }
            policy.guardrails_json = guardrails
            fallback_chain = list(policy.fallback_chain_json or [])
            policy.fallback_chain_json = [
                *fallback_chain,
                *(model_id for model_id in fallback if model_id not in fallback_chain),
            ]
        policies_by_name[name] = policy

    application_specs = (
        ("property-intelligence", "Property Intelligence"),
        ("support-copilot", "Support Copilot"),
        ("contract-review", "Contract Review"),
    )
    applications_by_slug: dict[str, Application] = {}
    for slug, name in application_specs:
        application = await session.scalar(select(Application).where(Application.slug == slug))
        if application is None:
            read = await ApplicationService(session).create(
                owner_id,
                ApplicationCreate(slug=slug, name=name, tags=["seeded", "gateway"]),
                initialize_gateway=False,
            )
            application = await session.get(Application, read.id)
        applications_by_slug[slug] = application

    first = applications_by_slug["property-intelligence"]
    config = await session.scalar(
        select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == first.id)
    )
    if config is None:
        await GatewayConfigurationService(session).upsert(
            owner_id,
            first.id,
            GatewayConfigUpsert(
                routing_policy_id=policies_by_name["general"].id,
                fallback_model_id=models_by_name[configured_by_cost[0].name].id,
            ),
        )

    # Persist updates to existing model metadata, memberships and policies;
    # catalog create methods may commit inserts, but in-place changes do not.
    await session.commit()

    counts = {
        "providers": int(await session.scalar(select(func.count()).select_from(Provider)) or 0),
        "models": int(await session.scalar(select(func.count()).select_from(Model)) or 0),
        "pools": int(await session.scalar(select(func.count()).select_from(ModelPool)) or 0),
        "policies": int(await session.scalar(select(func.count()).select_from(RoutingPolicy)) or 0),
        "applications": int(await session.scalar(select(func.count()).select_from(Application)) or 0),
        "configurations": int(await session.scalar(select(func.count()).select_from(ApplicationGatewayConfig)) or 0),
    }
    return SeedSummary(**counts)


async def seed_database(database_url: str | None = None, owner_id: str = SEED_OWNER_ID) -> SeedSummary:
    engine = create_engine_from_settings(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        summary = await seed_session(session, owner_id=owner_id)
    await engine.dispose()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Core/Gateway database")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--owner-id", default=SEED_OWNER_ID)
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    summary = asyncio.run(seed_database(args.database_url, owner_id=args.owner_id))
    print(summary)


if __name__ == "__main__":
    main()
