from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.core import Application
from src.shared.models.gateway import (
    ApplicationGatewayConfig,
    Model,
    ModelPool,
    ModelPoolMember,
    Provider,
    RoutingPolicy,
)
from src.shared.models.provider_credentials import ProviderCredential
from tests.service_app import app


def _feature_metadata_payload():
    return {
        "schema_version": 1,
        "upstream_features": {
            "chat": {
                "support": "supported",
                "conditions": [],
                "source_url": "https://platform.claude.com/docs/en/api/messages/create",
            }
        },
        "gateway_features": {
            "chat": {
                "support": "supported",
                "conditions": [],
                "source_url": "https://platform.claude.com/docs/en/api/messages/create",
            }
        },
        "constraints": {
            "thinking_mode": "adaptive",
            "default_effort": "medium",
            "forced_tool_choice": "conditional",
            "custom_sampling": False,
            "prompt_caching": True,
            "token_counting": True,
        },
        "pricing": {
            "currency": "USD",
            "standard_input_per_million": 0.1,
            "standard_output_per_million": 0.5,
            "long_context_input_threshold": 100000,
            "long_context_input_per_million": 0.5,
            "long_context_output_per_million": 2.5,
            "cache_write_5m_input_per_million": 0.125,
            "cache_write_1h_input_per_million": 0.2,
            "cache_read_input_per_million": 0.01,
        },
        "lifecycle": {
            "stage": "active",
            "reliable_knowledge_cutoff": "2026-06",
            "retirement_commitment": "2027-10-07",
        },
        "provenance": {
            "checked_on": "2026-10-17",
            "verification": "documentation_only",
            "sources": ["https://platform.claude.com/docs/en/models/overview"],
        },
        "latency_reference": {
            "source": "openrouter",
            "provider": "Anthropic",
            "service_tier": "standard",
            "source_metric": "latency_p50",
            "metric_interpretation": "ttft",
            "latency_ms": 1820,
            "throughput_tokens_per_second": 177,
            "percentile": 50,
            "reasoning_effort": "all",
            "locations": "all",
            "window": "1 week",
            "retrieved_on": "2026-10-17",
            "source_url": "https://openrouter.ai/anthropic/claude-haiku-5.5/",
            "verification": "external_reference",
        },
    }


def test_feature_metadata_round_trips_and_rejects_invalid_support_or_prices():
    from pydantic import ValidationError

    from src.shared.dtos.model_features import ModelFeatureMetadata

    metadata = ModelFeatureMetadata.model_validate(_feature_metadata_payload())
    assert metadata.model_dump(mode="json")["latency_reference"]["latency_ms"] == 1820

    invalid_support = _feature_metadata_payload()
    invalid_support["upstream_features"]["chat"]["support"] = "probably"
    with pytest.raises(ValidationError):
        ModelFeatureMetadata.model_validate(invalid_support)

    invalid_price = _feature_metadata_payload()
    invalid_price["pricing"]["standard_input_per_million"] = -0.1
    with pytest.raises(ValidationError):
        ModelFeatureMetadata.model_validate(invalid_price)


def test_model_dto_accepts_optional_features_and_output_limit():
    from src.backend.dtos.catalog import ModelCreate, ModelUpdate

    metadata = _feature_metadata_payload()
    create = ModelCreate(
        provider_id="provider-1",
        name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5",
        max_output_tokens=128000,
        feature_metadata=metadata,
    )
    update = ModelUpdate(max_output_tokens=64000, feature_metadata=metadata)

    assert create.status == "inactive"
    assert create.max_output_tokens == 128000
    assert update.max_output_tokens == 64000


def test_catalog_read_separates_baseline_and_probe_latency_and_keeps_legacy_nulls():
    from src.backend.services.model_catalog_service import ModelCatalogService
    from src.shared.models.gateway import Model, Provider

    provider = Provider(id="provider-1", name="anthropic", display_name="Anthropic", kind="anthropic")
    model = Model(
        id="model-1", provider_id=provider.id, name="claude-haiku-5-5", display_name="Claude Haiku 5.5",
        status="inactive", quality_tier="simple", health_status="unknown",
        target_latency_ms=None, last_probe_latency_ms=128.5, max_output_tokens=128000,
        input_price_per_million=0.1, output_price_per_million=0.5, pricing_version="test",
        feature_metadata_json=_feature_metadata_payload(),
    )
    read = ModelCatalogService._read(model, provider, 0)

    assert read.target_latency_ms is None
    assert read.last_probe_latency_ms == 128.5
    assert read.max_output_tokens == 128000
    assert read.feature_metadata.latency_reference.latency_ms == 1820

    legacy = Model(
        id="legacy", provider_id=provider.id, name="legacy", display_name="Legacy",
        status="inactive", quality_tier="standard", health_status="unknown",
        input_price_per_million=0, output_price_per_million=0, pricing_version="old",
        max_output_tokens=None, feature_metadata_json=None,
    )
    legacy_read = ModelCatalogService._read(legacy, provider, 0)
    assert legacy_read.max_output_tokens is None
    assert legacy_read.feature_metadata is None


@pytest.mark.asyncio
async def test_request_profile_changes_invalidate_probe_but_price_changes_do_not():
    from datetime import UTC, datetime

    from src.backend.dtos.catalog import ModelUpdate
    from src.backend.services.model_catalog_service import ModelCatalogService
    from src.shared.models.gateway import Model, Provider

    class Result:
        def __init__(self, value):
            self.value = value

        def one_or_none(self):
            return self.value

    class Session:
        def __init__(self, pair):
            self.pair = pair

        async def execute(self, *_args, **_kwargs):
            return Result(self.pair)

        async def commit(self):
            return None

        async def refresh(self, _model):
            return None

    provider = Provider(id="provider-1", name="anthropic", display_name="Anthropic", kind="anthropic")
    original = _feature_metadata_payload()
    model = Model(
        id="model-1", provider_id=provider.id, name="claude-haiku-5-5", display_name="Claude Haiku 5.5",
        status="active", health_status="healthy", last_probe_at=datetime.now(UTC),
        quality_tier="simple",
        last_probe_credential_version=3, max_output_tokens=128000, feature_metadata_json=original,
        input_price_per_million=0.1, output_price_per_million=0.5, pricing_version="test",
    )
    service = ModelCatalogService(Session((model, provider)))
    changed_constraints = _feature_metadata_payload()
    changed_constraints["constraints"]["thinking_mode"] = "always_on"

    await service.update(model.id, ModelUpdate(feature_metadata=changed_constraints))

    assert model.status == "inactive"
    assert model.health_status == "unknown"
    assert model.last_probe_credential_version is None

    model.status = "active"
    model.health_status = "healthy"
    model.last_probe_at = datetime.now(UTC)
    model.last_probe_credential_version = 3
    changed_price = _feature_metadata_payload()
    changed_price["pricing"]["standard_input_per_million"] = 0.2
    changed_price["constraints"]["thinking_mode"] = "always_on"

    await service.update(model.id, ModelUpdate(feature_metadata=changed_price))

    assert model.status == "active"
    assert model.health_status == "healthy"
    assert model.last_probe_credential_version == 3


@pytest.mark.asyncio
async def test_scalar_price_edit_updates_effective_anthropic_pricing_for_runtime_and_comparison():
    from src.backend.dtos.catalog import ModelUpdate
    from src.backend.services.model_catalog_service import ModelCatalogService
    from src.backend_ai.services.comparison_service import ComparisonExecutionService
    from src.shared.core.pricing import ModelPricing, PricingRegistry, pricing_rules_from_metadata
    from src.shared.dtos.gateway import OutputEvidence
    from src.shared.models.gateway import Model, Provider

    class Result:
        def one_or_none(self):
            return model, provider

    class Session:
        async def execute(self, *_args, **_kwargs):
            return Result()

        async def commit(self):
            return None

        async def refresh(self, _model):
            return None

    provider = Provider(id="provider-1", name="anthropic", display_name="Anthropic", kind="anthropic")
    model = Model(id="model-1", provider_id=provider.id, name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", status="inactive", health_status="unknown", quality_tier="simple",
        input_price_per_million=0.1, output_price_per_million=0.5, pricing_version="old",
        feature_metadata_json=_feature_metadata_payload())

    await ModelCatalogService(Session()).update(model.id,
        ModelUpdate(input_price_per_million=0.2, output_price_per_million=0.8))

    assert model.feature_metadata_json["pricing"]["standard_input_per_million"] == 0.2
    assert model.feature_metadata_json["pricing"]["standard_output_per_million"] == 0.8
    assert model.feature_metadata_json["pricing"]["long_context_input_per_million"] == 1.0
    assert model.feature_metadata_json["pricing"]["long_context_output_per_million"] == 4.0
    assert model.feature_metadata_json["pricing"]["cache_read_input_per_million"] == 0.02
    rules = pricing_rules_from_metadata(model.feature_metadata_json)
    runtime_cost = PricingRegistry([ModelPricing(model_id=model.id, provider="anthropic",
        input_price_per_million=model.input_price_per_million,
        output_price_per_million=model.output_price_per_million, pricing_version=model.pricing_version,
        pricing_rules=rules)]).calculate_usage(model.id, 100_001, 1_000, OutputEvidence(cached_tokens=100_000))
    comparison = ComparisonExecutionService(None, Settings(_env_file=None,
        database_url="sqlite+aiosqlite:///:memory:"))
    snapshot = comparison._model_snapshot(model, provider)
    comparison_cost = comparison._calculate_snapshot_cost(snapshot, 100_001, 1_000,
        OutputEvidence(cached_tokens=100_000))

    assert runtime_cost[0] == comparison_cost[0] == pytest.approx(0.014001)


@pytest.mark.asyncio
async def test_anthropic_model_with_stale_probe_credential_version_cannot_activate(monkeypatch):
    from datetime import UTC, datetime

    from src.backend.dtos.catalog import ModelUpdate
    from src.backend.services import model_catalog_service
    from src.backend.services.catalog_service import CatalogReferenceError
    from src.backend.services.model_catalog_service import ModelCatalogService
    from src.shared.models.gateway import Model, Provider
    from src.shared.models.provider_credentials import ProviderCredential

    class Result:
        def one_or_none(self):
            return model, provider

    class Session:
        async def execute(self, *_args, **_kwargs):
            return Result()

        async def scalar(self, *_args, **_kwargs):
            return ProviderCredential(provider_id=provider.id, encrypted_api_key="ciphertext", version=8,
                status="active")

        async def commit(self):
            return None

        async def refresh(self, _model):
            return None

    provider = Provider(id="anthropic-provider", name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active", base_url="https://api.anthropic.com")
    model = Model(id="anthropic-model", provider_id=provider.id, name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", status="inactive", health_status="healthy",
        last_probe_at=datetime.now(UTC), last_probe_credential_version=7, max_output_tokens=128000,
        feature_metadata_json=_feature_metadata_payload(), capabilities_json=["chat"])
    monkeypatch.setattr(model_catalog_service, "get_settings", lambda: Settings(_env_file=None,
        database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode="live"))

    with pytest.raises(CatalogReferenceError, match="current provider credential"):
        await ModelCatalogService(Session()).update(model.id, ModelUpdate(status="active"))


@pytest.mark.asyncio
async def test_anthropic_model_with_current_probe_credential_can_activate_and_be_callable(monkeypatch):
    from datetime import UTC, datetime

    from src.backend.dtos.catalog import ModelUpdate
    from src.backend.services import model_catalog_service
    from src.backend.services.model_catalog_service import ModelCatalogService
    from src.backend.services.pool_rules import callable_provider_ids
    from src.shared.models.gateway import Model, Provider
    from src.shared.models.provider_credentials import ProviderCredential

    class Result:
        def one_or_none(self):
            return model, provider

    class Session:
        async def execute(self, *_args, **_kwargs):
            return Result()

        async def scalar(self, *_args, **_kwargs):
            return ProviderCredential(provider_id=provider.id, encrypted_api_key="ciphertext", version=8,
                status="active")

        async def commit(self):
            return None

        async def refresh(self, _model):
            return None

    provider = Provider(id="anthropic-provider", name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active", base_url="https://api.anthropic.com")
    model = Model(id="anthropic-model", provider_id=provider.id, name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", status="inactive", health_status="healthy",
        last_probe_at=datetime.now(UTC), last_probe_credential_version=8, quality_tier="simple",
        context_window=1_000_000, max_output_tokens=128000,
        feature_metadata_json=_feature_metadata_payload(), capabilities_json=["chat"],
        input_price_per_million=0.1, output_price_per_million=0.5, pricing_version="test")
    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:",
        gateway_execution_mode="live", gateway_anthropic_enabled=True)
    monkeypatch.setattr(model_catalog_service, "get_settings", lambda: settings)

    await ModelCatalogService(Session()).update(model.id, ModelUpdate(status="active"))

    assert model.status == "active"
    assert callable_provider_ids(settings, {provider.id}, {provider.id: provider.kind}) == {provider.id}


@pytest_asyncio.fixture
async def catalog_client(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "catalog.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    permissions = {"models.read_available", "models.read", "models.manage"}

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="user-1",
            email="user@example.com",
            roles=("admin",),
            permissions=frozenset(permissions),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory, permissions
    app.dependency_overrides.clear()
    await engine.dispose()


async def seed_catalog(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    async with factory() as session:
        openai = Provider(
            id=str(uuid4()),
            name="openai",
            display_name="OpenAI",
            kind="openai",
            secret_env_var="OPENAI_API_KEY",
        )
        anthropic = Provider(
            id=str(uuid4()),
            name="anthropic",
            display_name="Anthropic",
            kind="anthropic",
        )
        gpt = Model(
            id=str(uuid4()),
            provider_id=openai.id,
            name="gpt-4.1",
            display_name="GPT-4.1",
            capabilities_json=["chat", "reasoning"],
            context_window=128000,
            target_latency_ms=900,
            input_price_per_million=2.0,
            output_price_per_million=8.0,
            quality_tier="premium",
            health_status="healthy",
        )
        legacy = Model(
            id=str(uuid4()),
            provider_id=openai.id,
            name="legacy-model",
            display_name="Legacy model",
            status="inactive",
            capabilities_json=["chat"],
        )
        claude = Model(
            id=str(uuid4()),
            provider_id=anthropic.id,
            name="claude-sonnet",
            display_name="Claude Sonnet",
            capabilities_json=["chat"],
            context_window=200000,
            target_latency_ms=1100,
            quality_tier="premium",
            health_status="unknown",
        )
        pool = ModelPool(id=str(uuid4()), name="support", purpose="Support", owner_user_id="user-1")
        member = ModelPoolMember(id=str(uuid4()), pool_id=pool.id, model_id=gpt.id)
        policy = RoutingPolicy(
            id=str(uuid4()),
            name="support-default",
            match_conditions_json={},
            guardrails_json={},
            fallback_chain_json=[],
        )
        application = Application(
            id=str(uuid4()),
            slug="support",
            name="Support",
            owner_user_id="user-1",
        )
        config = ApplicationGatewayConfig(
            id=str(uuid4()),
            application_id=application.id,
            model_pool_id=pool.id,
            routing_policy_id=policy.id,
        )
        session.add_all(
            [
                openai,
                ProviderCredential(
                    provider_id=openai.id,
                    encrypted_api_key="test-ciphertext",
                    status="active",
                ),
                anthropic,
                gpt,
                legacy,
                claude,
                pool,
                member,
                policy,
                application,
                config,
            ]
        )
        await session.commit()
        return {"gpt": gpt.id, "legacy": legacy.id, "claude": claude.id}


@pytest.mark.asyncio
async def test_available_catalog_filters_and_reports_used_by_count(catalog_client, monkeypatch):
    from src.backend.services import model_catalog_service

    monkeypatch.setattr(
        model_catalog_service,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            gateway_execution_mode="live",
            gateway_openai_enabled=True,
        ),
    )
    client, factory, _ = catalog_client
    ids = await seed_catalog(factory)

    response = await client.get(
        "/api/v1/gateway/available-models",
        params={"provider": "openai", "status": "active", "search": "GPT"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0] == {
        "id": ids["gpt"],
        "provider_id": payload["items"][0]["provider_id"],
        "provider": "OpenAI",
        "name": "gpt-4.1",
        "display_name": "GPT-4.1",
        "status": "active",
        "capabilities": ["chat", "reasoning"],
        "context_window": 128000,
        "target_latency_ms": 900,
        "quality_tier": "premium",
        "pricing": {
            "input_per_million": 2.0,
            "output_per_million": 8.0,
            "version": "1",
        },
        "health_status": "healthy",
        "last_probe_at": None,
        "last_probe_credential_version": None,
        "used_by_count": 1,
    }


@pytest.mark.asyncio
async def test_anthropic_stays_in_admin_catalog_but_available_catalog_requires_flag_and_key(catalog_client,
                                                                                           monkeypatch):
    from src.backend.services import model_catalog_service
    from src.shared.models.gateway import Provider

    monkeypatch.setattr(model_catalog_service, "get_settings", lambda: Settings(_env_file=None,
        database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode="live", gateway_anthropic_enabled=True))
    client, factory, _ = catalog_client
    ids = await seed_catalog(factory)

    admin = await client.get("/api/v1/gateway/models", params={"provider": "anthropic"})
    available_without_key = await client.get("/api/v1/gateway/available-models", params={"provider": "anthropic"})

    assert admin.status_code == 200 and admin.json()["total"] == 1
    assert admin.json()["items"][0]["id"] == ids["claude"]
    assert available_without_key.status_code == 200 and available_without_key.json()["total"] == 0
    async with factory() as session:
        provider = await session.get(Provider, admin.json()["items"][0]["provider_id"])
        session.add(ProviderCredential(provider_id=provider.id, encrypted_api_key="test-ciphertext", status="active"))
        await session.commit()

    available_with_key = await client.get("/api/v1/gateway/available-models", params={"provider": "anthropic"})
    assert available_with_key.status_code == 200 and available_with_key.json()["total"] == 1


@pytest.mark.asyncio
async def test_catalog_accepts_multiple_sort_criteria(catalog_client):
    client, factory, _ = catalog_client
    await seed_catalog(factory)

    response = await client.get(
        "/api/v1/gateway/models",
        params={"sort_by": "status,used_by", "sort_dir": "asc,desc", "page_size": 10},
    )

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["gpt-4.1", "claude-sonnet", "legacy-model"]


@pytest.mark.asyncio
async def test_admin_catalog_can_filter_inactive_models_and_enforces_permissions(catalog_client):
    client, factory, _ = catalog_client
    await seed_catalog(factory)

    inactive = await client.get("/api/v1/gateway/models", params={"status": "inactive", "page_size": 10})
    assert inactive.status_code == 200
    assert inactive.json()["total"] == 1
    assert inactive.json()["items"][0]["name"] == "legacy-model"

    _, _, permissions = catalog_client
    permissions.clear()
    denied_available = await client.get("/api/v1/gateway/available-models")
    denied_admin = await client.get("/api/v1/gateway/models")
    assert denied_available.status_code == 403
    assert denied_admin.status_code == 403
