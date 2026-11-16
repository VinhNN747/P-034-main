import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.dtos.applications import ApplicationCreate
from src.backend.dtos.catalog import ModelCreate, ModelPoolCreate, ModelPoolMemberCreate, PolicyCreate, ProviderCreate
from src.backend.dtos.gateway_configuration import GatewayConfigUpsert
from src.backend.services.application_service import ApplicationService
from src.backend.services.catalog_service import CatalogService
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.backend_ai.dtos.gateway_runtime import GatewayRuntimeInput
from src.backend_ai.services.comparison_service import ComparisonExecutionService
from src.backend_ai.services.gateway_execution_service import GatewayExecutionService
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.dtos.gateway import OutputEvidence
from src.shared.models.comparison import ComparisonBranch, ComparisonRun
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPool, ModelPoolMember, Provider
from src.shared.models.runtime import GatewayRequestRecord
from src.shared.services.comparison_query_service import ComparisonQueryService
from tests.service_app import app


def test_comparison_snapshot_uses_the_same_cache_and_long_context_pricing_rules():
    from src.shared.core.pricing import ModelPricing, PricingRegistry
    from src.shared.models.gateway import Model, Provider

    rules = {
        "currency": "USD",
        "standard_input_per_million": 0.10,
        "standard_output_per_million": 0.50,
        "long_context_input_threshold": 100_000,
        "long_context_threshold_operator": ">",
        "long_context_input_per_million": 0.50,
        "long_context_output_per_million": 2.50,
        "cache_write_5m_input_per_million": 0.125,
        "cache_write_1h_input_per_million": 0.20,
        "cache_read_input_per_million": 0.01,
        "batch_discount_fraction": 0.5,
    }
    provider = Provider(id="anthropic-provider", name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active")
    model = Model(id="haiku-id", provider_id=provider.id, name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5", context_window=1_000_000, input_price_per_million=0.10,
        output_price_per_million=0.50, pricing_version="anthropic-docs-2026-10-17",
        feature_metadata_json={"pricing": rules})
    service = ComparisonExecutionService(
        None, Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:")
    )
    snapshot = service._model_snapshot(model, provider)
    evidence = OutputEvidence(cached_tokens=100_000)

    comparison_cost = service._calculate_snapshot_cost(snapshot, 100_001, 1_000, evidence)
    runtime_cost = PricingRegistry(
        [ModelPricing(
            model_id=model.id, provider="anthropic", input_price_per_million=0.10,
            output_price_per_million=0.50, pricing_version="anthropic-docs-2026-10-17",
            pricing_rules=rules, estimated=True,
        )]
    ).calculate_usage(model.id, 100_001, 1_000, evidence)

    assert comparison_cost == runtime_cost
    assert comparison_cost[0] == pytest.approx(0.0075005)


def test_anthropic_comparison_schema_uses_supported_constraints_and_keeps_app_validation():
    run = type("Run", (), {"rubric_json": {"criteria": {"accuracy": {"weight": 1}}}})()
    labels = {"response_1": type("Branch", (), {"id": "branch-1"})()}
    schema = ComparisonExecutionService._evaluation_schema(run, labels)

    def keywords(value):
        if isinstance(value, dict):
            return set(value).union(*(keywords(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keywords(item) for item in value))
        return set()

    assert not {"minimum", "maximum", "maxLength"}.intersection(keywords(schema))
    assert schema["properties"]["scores"]["properties"]["response_1"]["required"] == [
        "total", "criteria", "rationale"]


@pytest.mark.parametrize("payload, message", [
    ({"scores": {"response_1": {"total": 101, "criteria": {"accuracy": 50}, "rationale": "ok"}}},
     "total score"),
    ({"scores": {"response_1": {"total": 50, "criteria": {}, "rationale": "ok"}}}, "criterion scores"),
    ({"scores": {"response_1": {"total": 50, "criteria": {"accuracy": 50},
        "rationale": "x" * 1001}}}, "rationale"),
])
def test_comparison_evaluator_independently_validates_schema_constraints(payload, message):
    label_map = {"response_1": type("Branch", (), {"id": "branch-1"})()}

    with pytest.raises(ValueError, match=message):
        ComparisonExecutionService._validated_scores(payload, label_map, {"accuracy"})


@pytest.mark.asyncio
async def test_comparison_budget_counts_haiku_prompt_or_uses_highest_tier_price():
    service = ComparisonExecutionService(
        None, Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:")
    )
    snapshot = {
        "id": "haiku-id",
        "name": "claude-haiku-5-5",
        "provider_kind": "anthropic",
        "input_price_per_million": 0.10,
        "output_price_per_million": 0.50,
        "pricing_version": "anthropic-docs-2026-10-17",
        "pricing_rules": {
            "standard_input_per_million": 0.10,
            "standard_output_per_million": 0.50,
            "long_context_input_threshold": 100_000,
            "long_context_threshold_operator": ">",
            "long_context_input_per_million": 0.50,
            "long_context_output_per_million": 2.50,
            "cache_write_5m_input_per_million": 0.125,
            "cache_write_1h_input_per_million": 0.20,
            "cache_read_input_per_million": 0.01,
        },
        "timeout_seconds": 2,
    }

    fallback_tokens, use_highest = await service._input_tokens_for_budget(
        object(), snapshot, "x", max_tokens=1_000, task_id="budget-fallback"
    )
    fallback_uses_highest = use_highest
    fallback_cost, _ = service._calculate_snapshot_cost(
        snapshot, fallback_tokens, 1_000, force_highest_tier=use_highest
    )

    class Counter:
        async def count_tokens(self, prompt, **kwargs):
            assert prompt == "x"
            assert kwargs["task_id"] == "budget-count"
            return 100_000

    exact_tokens, use_highest = await service._input_tokens_for_budget(
        Counter(), snapshot, "x", max_tokens=1_000, task_id="budget-count"
    )
    exact_cost, _ = service._calculate_snapshot_cost(
        snapshot, exact_tokens, 1_000, force_highest_tier=use_highest
    )

    assert (fallback_tokens, fallback_uses_highest) == (1, True)
    assert use_highest is False
    assert fallback_cost == pytest.approx(0.0025005)
    assert exact_tokens == 100_000
    assert exact_cost == pytest.approx(0.0105)


@pytest_asyncio.fixture
async def comparison_client(tmp_path: Path, monkeypatch):
    from src.backend_ai.routers import comparisons as comparison_router

    settings = Settings(_env_file=None, gateway_execution_mode="offline")
    monkeypatch.setattr(
        comparison_router, "ComparisonExecutionService", lambda session: ComparisonExecutionService(session, settings)
    )
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "comparison.db"))

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        application = await ApplicationService(session).create(
            "user-1", ApplicationCreate(slug="comparison", name="Comparison")
        )
        catalog = CatalogService(session)
        provider = await catalog.create_provider(ProviderCreate(name="local", display_name="Local", kind="offline"))
        model = await catalog.create_model(
            ModelCreate(
                provider_id=provider.id,
                name="local-deterministic",
                display_name="Local deterministic",
                capabilities=["chat", "text", "long_context"],
                input_price_per_million=1.0,
                output_price_per_million=2.0,
            )
        )
        pool = await catalog.create_pool(
            ModelPoolCreate(
                name="comparison-pool",
                purpose="comparison",
                members=[ModelPoolMemberCreate(model_id=model.id)],
            ),
            "user-1",
        )
        policy = await catalog.create_policy(PolicyCreate(name="comparison-policy"))
        await GatewayConfigurationService(session).upsert(
            "user-1",
            application.id,
            GatewayConfigUpsert(model_pool_id=pool.id, routing_policy_id=policy.id),
        )

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(id="user-1", email="user@example.com", roles=("owner",), permissions=frozenset())

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, factory, settings, application.id, model.id
    app.dependency_overrides.clear()
    await engine.dispose()


def install_provider_builder(monkeypatch, module, provider_client):
    class TestProviderBuilder:
        def __init__(self, _resolver, _settings):
            self.available_provider_ids = set()
            self.credential_versions = {}

        async def build_for_models(self, rows):
            clients = {}
            for model, provider in rows:
                self.available_provider_ids.add(provider.id)
                clients[model.id] = provider_client
                clients[provider.name] = provider_client
            return clients

    monkeypatch.setattr(module, "ProviderClientBuilder", TestProviderBuilder)


@pytest.mark.asyncio
async def test_comparison_worker_resolves_current_key_by_snapshot_model_id(comparison_client):
    from cryptography.fernet import Fernet

    from src.backend.dtos.catalog import ModelCreate, ProviderCreate
    from src.backend.services.catalog_service import CatalogService
    from src.shared.models.provider_credentials import ProviderCredential
    from src.shared.services.credential_crypto import CredentialCrypto

    _, factory, _, _, _ = comparison_client
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        gateway_execution_mode="live",
        gateway_openai_enabled=True,
        credential_encryption_key=Fernet.generate_key().decode(),
    )
    crypto = CredentialCrypto.from_settings(settings)
    async with factory() as session:
        catalog = CatalogService(session)
        provider = await catalog.create_provider(
            ProviderCreate(name="comparison-openai", display_name="Comparison OpenAI", kind="openai")
        )
        model = await catalog.create_model(
            ModelCreate(provider_id=provider.id, name="gpt-4o-mini", display_name="GPT Mini", capabilities=["chat"])
        )
        credential = ProviderCredential(
            id="comparison-credential",
            provider_id=provider.id,
            encrypted_api_key=crypto.encrypt(
                provider_id=provider.id,
                credential_id="comparison-credential",
                version=1,
                api_key="first-db-key",
            ),
            version=1,
            status="active",
        )
        session.add(credential)
        await session.commit()
        service = ComparisonExecutionService(session, settings)
        model_row = await session.get(Model, model.id)
        snapshot = service._model_snapshot(model_row, provider)
        credential.version = 2
        credential.encrypted_api_key = crypto.encrypt(
            provider_id=provider.id,
            credential_id=credential.id,
            version=2,
            api_key="rotated-db-key",
        )
        await session.commit()

        client = await service._provider_for_snapshot(snapshot)

    assert client is not None
    assert client._api_key == "rotated-db-key"


@pytest.mark.asyncio
async def test_comparison_runs_gateway_once_and_full_prompt_reference(comparison_client):
    client, factory, settings, application_id, model_id = comparison_client
    payload = {
        "application_id": application_id,
        "prompt": "Write a concise greeting",
        "reference_model_ids": [model_id],
        "idempotency_key": "comparison-1",
    }
    created = await client.post("/api/v1/gateway/comparisons", json=payload)
    assert created.status_code == 202, created.text
    comparison_id = created.json()["comparison_id"]

    replayed = await client.post("/api/v1/gateway/comparisons", json=payload)
    assert replayed.status_code == 202
    assert replayed.json() == {"comparison_id": comparison_id, "status": "queued", "replayed": True}

    async with factory() as session:
        service = ComparisonExecutionService(session, settings)
        assert await service.claim_next() == comparison_id
    async with factory() as session:
        await ComparisonExecutionService(session, settings).process(comparison_id)

    response = await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "completed"
    assert report["gateway_request_id"]
    assert len(report["branches"]) == 2
    gateway = next(item for item in report["branches"] if item["branch_type"] == "gateway")
    reference = next(item for item in report["branches"] if item["branch_type"] == "reference")
    assert gateway["status"] == "completed"
    assert reference["status"] == "completed"
    assert reference["output"].startswith("offline:local-deterministic:comparison:")
    assert gateway["latency_ms"] is not None
    assert reference["latency_ms"] is not None
    assert report["comparison_spend_usd"] == pytest.approx(gateway["cost_usd"] + reference["cost_usd"])

    exported = await client.get(
        "/api/v1/gateway/comparisons/export.csv",
        params={"application_id": application_id},
    )
    assert exported.status_code == 200
    assert "text/csv" in exported.headers["content-type"]
    assert comparison_id in exported.text
    assert model_id in exported.text


@pytest.mark.asyncio
async def test_comparison_rejects_model_outside_application_pool(comparison_client):
    client, _, _, application_id, _ = comparison_client
    response = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Hello",
            "reference_model_ids": ["outside-model"],
            "idempotency_key": "comparison-outside",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_report", [True, False])
async def test_comparison_quality_evaluation_is_blinded_and_persisted(comparison_client, monkeypatch, valid_report):
    client, factory, settings, application_id, model_id = comparison_client
    from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse
    from src.backend_ai.services import comparison_service as comparison_module

    class EvaluationProvider(ProviderClient):
        name = "offline"

        async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
            if task_id.startswith("comparison-evaluation:"):
                assert options.json_schema["properties"]["scores"]["required"] == ["response_1", "response_2"]
                assert options.low_reasoning is True
                assert options.max_output_tokens == 4096
                text = (
                    '{"scores":{'
                    '"response_1":{"total":80,"criteria":{},"rationale":"clear"},'
                    '"response_2":{"total":70,"criteria":{},"rationale":"adequate"}}}'
                )
                if not valid_report:
                    text = "invalid evaluator response"
            else:
                text = "direct full-prompt answer"
            return ProviderResponse(
                text=text,
                provider="offline",
                model_id=model_id,
                input_tokens=10,
                output_tokens=5,
                latency_ms=1,
                usage_received=True,
            )

    provider = EvaluationProvider()
    install_provider_builder(monkeypatch, comparison_module, provider)
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Write a concise greeting",
            "reference_model_ids": [model_id],
            "quality_evaluation": True,
            "idempotency_key": "comparison-quality",
        },
    )
    comparison_id = created.json()["comparison_id"]
    async with factory() as session:
        service = ComparisonExecutionService(session, settings)
        await service.claim_next()
    async with factory() as session:
        await ComparisonExecutionService(session, settings).process(comparison_id)
    report = (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()
    assert report["evaluation_cost_usd"] > 0
    assert report["evaluation_usage_complete"] is True
    if not valid_report:
        assert report["status"] == "partial"
        assert report["evaluation_status"] == "failed"
        assert report["evaluation_error_code"] == "EVALUATION_INVALID_REPORT"
        assert report["evaluation_error_message"]
        assert report["deltas"][0]["comparable_quality"] is False
        return
    assert report["evaluation_status"] == "completed"
    assert sorted(branch["quality_score"] for branch in report["branches"]) == [70.0, 80.0]
    assert report["deltas"][0]["comparable_quality"] is True


@pytest.mark.asyncio
async def test_unreceipted_retry_is_not_reported_as_savings(comparison_client, monkeypatch):
    client, factory, settings, application_id, model_id = comparison_client
    from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse, TransientProviderError
    from src.backend_ai.services import comparison_service as comparison_module

    class RetryingProvider(ProviderClient):
        name = "offline"

        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
            self.calls += 1
            if self.calls == 1:
                raise TransientProviderError("receipt unavailable")
            return ProviderResponse(
                text="answer after retry",
                provider="offline",
                model_id=model_id,
                input_tokens=10,
                output_tokens=5,
                latency_ms=1,
                usage_received=True,
            )

    provider = RetryingProvider()
    install_provider_builder(monkeypatch, comparison_module, provider)
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Retry this direct prompt",
            "reference_model_ids": [model_id],
            "idempotency_key": "comparison-retry",
        },
    )
    comparison_id = created.json()["comparison_id"]
    async with factory() as session:
        await ComparisonExecutionService(session, settings).claim_next()
    async with factory() as session:
        await ComparisonExecutionService(session, settings).process(comparison_id)
    report = (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()
    reference = next(branch for branch in report["branches"] if branch["branch_type"] == "reference")
    assert provider.calls == 2
    assert reference["status"] == "completed"
    assert reference["usage_complete"] is False
    assert report["deltas"][0]["comparable_cost"] is False


@pytest.mark.asyncio
async def test_comparison_branches_execute_concurrently_in_independent_sessions(comparison_client, monkeypatch):
    client, factory, settings, application_id, model_id = comparison_client
    gateway_started = asyncio.Event()
    reference_started = asyncio.Event()
    gateway_method = ComparisonExecutionService._run_gateway
    reference_method = ComparisonExecutionService._run_reference
    branch_sessions = []

    async def gateway(self, run, branch):
        branch_sessions.append(self.session)
        gateway_started.set()
        await asyncio.wait_for(reference_started.wait(), timeout=2)
        await gateway_method(self, run, branch)

    async def reference(self, run, branch):
        branch_sessions.append(self.session)
        reference_started.set()
        await asyncio.wait_for(gateway_started.wait(), timeout=2)
        await reference_method(self, run, branch)

    monkeypatch.setattr(ComparisonExecutionService, "_run_gateway", gateway)
    monkeypatch.setattr(ComparisonExecutionService, "_run_reference", reference)
    response = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Compare these executions",
            "reference_model_ids": [model_id],
            "idempotency_key": "comparison-parallel",
            "execution_strategy": "parallel",
        },
    )
    comparison_id = response.json()["comparison_id"]
    async with factory() as session:
        await ComparisonExecutionService(session, settings).claim_next()
    async with factory() as session:
        await ComparisonExecutionService(session, settings).process(comparison_id)
    report = (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()
    assert report["status"] == "completed"
    assert len(branch_sessions) == 2 and branch_sessions[0] is not branch_sessions[1]


@pytest.mark.asyncio
async def test_comparison_runs_sequentially_before_evaluation(comparison_client, monkeypatch):
    client, factory, settings, application_id, model_id = comparison_client
    stages = []
    original_gateway = ComparisonExecutionService._run_gateway
    original_reference = ComparisonExecutionService._run_reference
    original_evaluate = ComparisonExecutionService._run_evaluation

    async def gateway(self, run, branch):
        stages.append("routing:start")
        await original_gateway(self, run, branch)
        stages.append("routing:end")

    async def reference(self, run, branch):
        assert stages[-1] == "routing:end"
        stages.append("reference:start")
        await original_reference(self, run, branch)
        stages.append("reference:end")

    async def evaluate(self, run, branches):
        assert stages[-1] == "reference:end"
        stages.append("evaluation")
        await original_evaluate(self, run, branches)

    monkeypatch.setattr(ComparisonExecutionService, "_run_gateway", gateway)
    monkeypatch.setattr(ComparisonExecutionService, "_run_reference", reference)
    monkeypatch.setattr(ComparisonExecutionService, "_run_evaluation", evaluate)
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "quality_evaluation": True,
            "idempotency_key": "sequential",
        },
    )
    assert created.status_code == 202, created.text
    async with factory() as session:
        await ComparisonExecutionService(session, settings).claim_next()
    async with factory() as session:
        await ComparisonExecutionService(session, settings).process(created.json()["comparison_id"])
    assert stages == ["routing:start", "routing:end", "reference:start", "reference:end", "evaluation"]


async def saved_routing(factory, settings, application_id):
    async with factory() as session:
        return await GatewayExecutionService(session, settings).run_legacy(
            GatewayRuntimeInput(
                application_id=application_id,
                prompt="Say hello",
                context={"name": "Example"},
                source="playground",
                decomposer="deterministic",
                aggregator="deterministic",
            ),
            CurrentUser(id="user-1", email=None, roles=("owner",), permissions=frozenset()),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("use_pool", [True, False])
@pytest.mark.parametrize("inactive", ["model", "provider"])
async def test_rejected_comparison_keeps_saved_routing(comparison_client, use_pool, inactive):
    client, factory, settings, application_id, model_id = comparison_client
    if not use_pool:
        async with factory() as session:
            await GatewayConfigurationService(session).upsert(
                "user-1", application_id, GatewayConfigUpsert(model_pool_id=None)
            )
    routing = await saved_routing(factory, settings, application_id)
    async with factory() as session:
        model = await session.get(Model, model_id)
        target = model if inactive == "model" else await session.get(Provider, model.provider_id)
        target.status = "inactive"
        await session.commit()
    rejected = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "gateway_request_id": routing["request_id"],
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "idempotency_key": "inactive-comparison",
        },
    )
    assert rejected.status_code == 422, rejected.text
    async with factory() as session:
        receipt = await session.get(GatewayRequestRecord, routing["request_id"])
        assert receipt.status == "completed"
        assert receipt.response_json["aggregation"]["final_answer"] == routing["output"]
        assert await session.scalar(select(func.count()).select_from(GatewayRequestRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(ComparisonRun)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_pool", ["inactive", "other-owner"])
async def test_comparison_does_not_expand_invalid_pool_to_catalog(comparison_client, invalid_pool):
    client, factory, _, application_id, model_id = comparison_client
    async with factory() as session:
        config = await session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application_id)
        )
        pool = await session.get(ModelPool, config.model_pool_id)
        if invalid_pool == "inactive":
            pool.status = "inactive"
        else:
            pool.owner_user_id = "someone-else"
        await session.commit()
    rejected = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "idempotency_key": "invalid-pool",
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "Active model pool not found"


@pytest.mark.asyncio
@pytest.mark.parametrize("use_pool", [True, False])
async def test_comparison_reuses_saved_routing_and_preserves_context(comparison_client, monkeypatch, use_pool):
    client, factory, settings, application_id, model_id = comparison_client
    if not use_pool:
        async with factory() as session:
            await GatewayConfigurationService(session).upsert(
                "user-1", application_id, GatewayConfigUpsert(model_pool_id=None)
            )
    routing = await saved_routing(factory, settings, application_id)

    async def must_not_route_again(*args, **kwargs):
        raise AssertionError("Routing must not be executed again")

    monkeypatch.setattr(ComparisonExecutionService, "_run_gateway", must_not_route_again)
    payload = {
        "application_id": application_id,
        "gateway_request_id": routing["request_id"],
        "prompt": "Say hello",
        "reference_model_ids": [model_id],
        "idempotency_key": "reuse-routing",
    }
    created = await client.post("/api/v1/gateway/comparisons", json=payload)
    assert created.status_code == 202, created.text
    comparison_id = created.json()["comparison_id"]
    async with factory() as session:
        run = await session.get(ComparisonRun, comparison_id)
        assert run.context_json == {"name": "Example"}
        assert run.constraints_json["max_output_tokens"] > 0
        # The completed Gateway branch must be persisted before the queue can claim it.
        gateway = await session.scalar(
            select(ComparisonBranch).where(
                ComparisonBranch.comparison_id == comparison_id, ComparisonBranch.branch_type == "gateway"
            )
        )
        assert gateway.status == "completed" and gateway.output == routing["output"]
        await ComparisonExecutionService(session, settings).claim_next()
    async with factory() as session:
        await ComparisonExecutionService(session, settings).process(comparison_id)
        assert await session.scalar(select(func.count()).select_from(GatewayRequestRecord)) == 1
    report = (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()
    assert report["gateway_request_id"] == routing["request_id"]
    assert report["status"] == "completed"
    gateway = next(item for item in report["branches"] if item["branch_type"] == "gateway")
    assert gateway["cost_usd"] == routing["estimated_cost"]
    conflict = await client.post("/api/v1/gateway/comparisons", json={**payload, "prompt": "Changed prompt"})
    assert conflict.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,expected",
    [
        ({"prompt": "Changed prompt"}, 422),
        ({"gateway_request_id": "not-owned"}, 404),
        ({"context": {"name": "Wrong"}}, 422),
    ],
)
async def test_comparison_rejects_changed_or_inaccessible_receipt(comparison_client, change, expected):
    client, factory, settings, application_id, model_id = comparison_client
    routing = await saved_routing(factory, settings, application_id)
    result = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "gateway_request_id": routing["request_id"],
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "idempotency_key": "invalid-reuse",
            **change,
        },
    )
    assert result.status_code == expected, result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("use_pool", [True, False])
async def test_evaluator_can_differ_from_reference_model(comparison_client, monkeypatch, use_pool):
    client, factory, settings, application_id, model_id = comparison_client
    from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse
    from src.backend_ai.services import comparison_service as module

    async with factory() as session:
        provider_id = await session.scalar(select(Model.provider_id).where(Model.id == model_id))
        evaluator = await CatalogService(session).create_model(
            ModelCreate(
                provider_id=provider_id,
                name="quality-evaluator",
                display_name="Quality evaluator",
                capabilities=["text"],
            )
        )
        config = await session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application_id)
        )
        if use_pool:
            session.add(ModelPoolMember(pool_id=config.model_pool_id, model_id=evaluator.id))
            await session.commit()
        else:
            await GatewayConfigurationService(session).upsert(
                "user-1", application_id, GatewayConfigUpsert(model_pool_id=None)
            )
        evaluator_id = evaluator.id
    observed = []

    class Provider(ProviderClient):
        name = "offline"

        async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
            import json

            observed.append((task_id, model_id))
            text = (
                json.dumps({"scores": {"response_1": {"total": 80}, "response_2": {"total": 75}}})
                if task_id.startswith("comparison-evaluation:")
                else "Reference answer"
            )
            return ProviderResponse(
                text=text, provider="offline", model_id=model_id, usage_received=True, input_tokens=10, output_tokens=20
            )

    install_provider_builder(monkeypatch, module, Provider())
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "quality_evaluation": True,
            "evaluator_model_id": evaluator_id,
            "idempotency_key": "separate-evaluator",
        },
    )
    assert created.status_code == 202, created.text
    comparison_id = created.json()["comparison_id"]
    async with factory() as session:
        await ComparisonExecutionService(session, settings).claim_next()
    async with factory() as session:
        await ComparisonExecutionService(session, settings).process(comparison_id)
    report = (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()
    assert report["evaluation_status"] == "completed"
    assert report["evaluator_model_id"] == evaluator_id
    assert len(report["branches"]) == 2
    assert observed[-1] == (f"comparison-evaluation:{comparison_id}", "quality-evaluator")


def integrated_comparison_app(factory, settings, monkeypatch):
    from src.backend_ai.services.comparison_service import run_worker
    from src.backend_ai.workers import lifespan as module
    from src.shared.core import application as core

    local_settings = settings.model_copy(
        update={"comparison_worker_poll_seconds": 0.01, "comparison_worker_shutdown_seconds": 0.05}
    )
    monkeypatch.setattr(module, "get_settings", lambda: local_settings)
    monkeypatch.setattr(core, "engine", factory.kw["bind"])
    monkeypatch.setattr(core, "get_settings", lambda: local_settings)

    async def worker(poll_seconds, **kwargs):
        await run_worker(poll_seconds, session_factory=factory, **kwargs)

    monkeypatch.setattr(module, "run_worker", worker)
    return core.create_application("Comparison lifecycle test", runtime_context=module.comparison_lifespan)


@pytest.mark.asyncio
async def test_ai_lifespan_processes_comparison_without_separate_worker(comparison_client, monkeypatch):
    client, factory, settings, application_id, model_id = comparison_client
    ai = integrated_comparison_app(factory, settings, monkeypatch)
    async with ai.router.lifespan_context(ai):
        created = await client.post(
            "/api/v1/gateway/comparisons",
            json={
                "application_id": application_id,
                "prompt": "Say hello",
                "reference_model_ids": [model_id],
                "idempotency_key": "integrated-worker",
            },
        )
        assert created.status_code == 202, created.text
        async with asyncio.timeout(5):
            while True:
                async with factory() as session:
                    report = await ComparisonQueryService(session).get_owned(created.json()["comparison_id"], "user-1")
                if report.status == "completed":
                    break
                await asyncio.sleep(0.01)
        assert report.gateway_request_id
    assert ai.state.comparison_worker.done()


@pytest.mark.asyncio
async def test_shutdown_marks_interrupted_work_without_replaying(comparison_client, monkeypatch):
    client, factory, settings, application_id, model_id = comparison_client
    started = asyncio.Event()

    async def hang(self, run, branch):
        branch.status = "running"
        await self.session.commit()
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ComparisonExecutionService, "_run_reference", hang)
    ai = integrated_comparison_app(factory, settings, monkeypatch)
    async with ai.router.lifespan_context(ai):
        created = await client.post(
            "/api/v1/gateway/comparisons",
            json={
                "application_id": application_id,
                "prompt": "Say hello",
                "reference_model_ids": [model_id],
                "idempotency_key": "interrupted-worker",
            },
        )
        assert created.status_code == 202, created.text
        await asyncio.wait_for(started.wait(), timeout=5)
    async with factory() as session:
        run = await session.get(ComparisonRun, created.json()["comparison_id"])
        assert run.status == "indeterminate"
        assert run.lease_expires_at is None
        assert await ComparisonExecutionService(session, settings).claim_next() is None


@pytest.mark.asyncio
async def test_integrated_worker_can_be_disabled(monkeypatch):
    from fastapi import FastAPI

    from src.backend_ai.workers import lifespan as module

    settings = Settings(_env_file=None, comparison_worker_enabled=False)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    ai = FastAPI()
    async with module.comparison_lifespan(ai):
        assert not hasattr(ai.state, "comparison_worker")


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_the_same_comparison(comparison_client):
    client, factory, settings, application_id, model_id = comparison_client
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "idempotency_key": "claim-once",
        },
    )
    assert created.status_code == 202, created.text

    async def claim():
        async with factory() as session:
            return await ComparisonExecutionService(session, settings).claim_next()

    claimed = await asyncio.gather(claim(), claim())
    assert [item for item in claimed if item is not None] == [created.json()["comparison_id"]]


@pytest.mark.asyncio
async def test_queued_offline_comparison_cannot_become_live_on_restart(comparison_client, monkeypatch):
    client, factory, settings, application_id, model_id = comparison_client
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "idempotency_key": "offline-only",
        },
    )
    assert created.status_code == 202, created.text
    live_settings = settings.model_copy(update={"gateway_execution_mode": "live"})

    async def must_not_call_provider(*args, **kwargs):
        raise AssertionError("An offline job must not cause live provider calls")

    monkeypatch.setattr(ComparisonExecutionService, "_execute_branch", must_not_call_provider)
    async with factory() as session:
        service = ComparisonExecutionService(session, live_settings)
        await service.claim_next()
        await service.process(created.json()["comparison_id"])
    report = (await client.get(f"/api/v1/gateway/comparisons/{created.json()['comparison_id']}")).json()
    assert report["status"] == "failed"
    assert report["error_code"] == "EXECUTION_MODE_CHANGED"


@pytest.mark.asyncio
@pytest.mark.parametrize("hanging_stage", ["reference", "evaluation"])
async def test_comparison_provider_deadlines_finish_without_hanging(comparison_client, monkeypatch, hanging_stage):
    client, factory, settings, application_id, model_id = comparison_client
    from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse
    from src.backend_ai.services import comparison_service as comparison_module

    class HangingProvider(ProviderClient):
        name = "offline"

        async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
            stage = "evaluation" if task_id.startswith("comparison-evaluation:") else "reference"
            if stage == hanging_stage:
                await asyncio.Event().wait()
            return ProviderResponse(
                text="direct output",
                provider="offline",
                model_id=model_id,
                input_tokens=10,
                output_tokens=5,
                latency_ms=1,
                usage_received=True,
            )

    provider = HangingProvider()
    install_provider_builder(monkeypatch, comparison_module, provider)
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "prompt": "Check execution deadline",
            "reference_model_ids": [model_id],
            "quality_evaluation": True,
            "idempotency_key": "comparison-timeout-" + hanging_stage,
        },
    )
    comparison_id = created.json()["comparison_id"]
    async with factory() as session:
        run = await session.get(ComparisonRun, comparison_id)
        run.model_snapshot_json = [{**item, "timeout_seconds": 0.03} for item in run.model_snapshot_json]
        await session.commit()
        await ComparisonExecutionService(session, settings).claim_next()
    async with factory() as session:
        await asyncio.wait_for(ComparisonExecutionService(session, settings).process(comparison_id), timeout=5)
    report = (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()
    assert report["evaluation_status"] == "failed"
    if hanging_stage == "reference":
        assert report["status"] == "partial"
        reference = next(branch for branch in report["branches"] if branch["branch_type"] == "reference")
        assert reference["error_code"] == "TimeoutError"
        assert reference["usage_complete"] is False
    else:
        assert report["status"] == "partial"
        assert report["evaluation_error_code"] == "MODEL_TIMEOUT"
        assert "deadline" in report["evaluation_error_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_code",
    [
        ("thinking_limit", "OUTPUT_TOKEN_LIMIT"),
        ("rate_limit", "PROVIDER_HTTP_429"),
        ("missing_score", "EVALUATION_INVALID_REPORT"),
        ("boolean_score", "EVALUATION_INVALID_REPORT"),
    ],
)
async def test_evaluator_failure_preserves_answers_and_explains_reason(
    comparison_client, monkeypatch, failure, expected_code
):
    import json

    from src.backend_ai.gateway.providers.base import (
        ProviderClient,
        ProviderOutputError,
        ProviderResponse,
        TransientProviderError,
    )
    from src.backend_ai.services import comparison_service as module

    client, factory, settings, application_id, model_id = comparison_client
    routing = await saved_routing(factory, settings, application_id)
    calls = []

    class EvaluatorProvider(ProviderClient):
        async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
            calls.append(task_id)
            if not task_id.startswith("comparison-evaluation:"):
                return ProviderResponse(
                    text="Alternative answer",
                    provider="offline",
                    model_id=model_id,
                    input_tokens=5,
                    output_tokens=10,
                    usage_received=True,
                )
            if failure == "rate_limit":
                raise TransientProviderError("Gemini API returned HTTP 429: secret API_KEY=do-not-show")
            if failure == "thinking_limit":
                raise ProviderOutputError(
                    "OUTPUT_TOKEN_LIMIT",
                    "No answer",
                    ProviderResponse(
                        text="",
                        provider="offline",
                        model_id=model_id,
                        input_tokens=30,
                        output_tokens=1200,
                        usage_received=True,
                        metadata={"finish_reason": "MAX_TOKENS"},
                    ),
                )
            scores = {"response_1": {"total": 75}, "response_2": {"total": 80}}
            if failure == "missing_score":
                scores.pop("response_2")
            else:
                scores["response_1"]["total"] = True
            return ProviderResponse(
                text=json.dumps({"scores": scores}),
                provider="offline",
                model_id=model_id,
                input_tokens=30,
                output_tokens=40,
                usage_received=True,
            )

    install_provider_builder(monkeypatch, module, EvaluatorProvider())
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "gateway_request_id": routing["request_id"],
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "quality_evaluation": True,
            "evaluator_model_id": model_id,
            "idempotency_key": "failure-" + failure,
        },
    )
    assert created.status_code == 202, created.text
    comparison_id = created.json()["comparison_id"]
    async with factory() as session:
        service = ComparisonExecutionService(session, settings)
        await service.claim_next()
        await service.process(comparison_id)
    report = (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()
    assert report["status"] == "partial"
    assert report["evaluation_error_code"] == expected_code
    assert report["evaluation_error_message"].startswith("Quality evaluation failed:")
    assert "secret" not in report["evaluation_error_message"]
    assert "branch could not" not in report["evaluation_error_message"]
    assert [branch["status"] for branch in report["branches"]] == ["completed", "completed"]
    assert all(branch["quality_score"] is None for branch in report["branches"])
    assert report["branches"][0]["output"] == routing["output"]
    assert report["branches"][1]["output"] == "Alternative answer"
    assert report["deltas"][0]["comparable_cost"] is True
    assert report["deltas"][0]["comparable_quality"] is False
    assert len(calls) == 2  # Reference and evaluator are distinct calls; routing is reused.
    if failure == "thinking_limit":
        assert report["evaluation_total_tokens"] == 1230
        assert report["evaluation_cost_usd"] > 0
        assert report["evaluation_usage_complete"] is True
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(GatewayRequestRecord)) == 1
        run = await session.get(ComparisonRun, comparison_id)
        run.status = "completed"  # Historical reports also reflect the failed evaluator accurately.
        await session.commit()
    assert (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()["status"] == "partial"


@pytest.mark.asyncio
async def test_gemini_evaluation_runs_with_native_json_and_thinking_options(comparison_client, monkeypatch):
    import json

    import httpx

    from src.backend_ai.gateway.providers.gemini import GeminiProvider
    from src.backend_ai.services import comparison_service as module

    client, factory, settings, application_id, model_id = comparison_client
    routing = await saved_routing(factory, settings, application_id)
    async with factory() as session:
        model = await session.get(Model, model_id)
        model.name = "gemini-3.6-flash"
        await session.commit()
    calls = []

    def respond(request):
        body = json.loads(request.content)
        config = body["generationConfig"]
        calls.append(config)
        if "responseFormat" in config:
            assert config["thinkingConfig"] == {"thinkingLevel": "low"}
            assert config["maxOutputTokens"] == 8192  # native evaluator retains its reasoning headroom
            labels = config["responseFormat"]["text"]["schema"]["properties"]["scores"]["required"]
            text = json.dumps(
                {"scores": {label: {"total": 80, "criteria": {}, "rationale": "Clear"} for label in labels}}
            )
        else:
            text = "Alternative answer"
        return httpx.Response(
            200,
            json={
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
                "usageMetadata": {"promptTokenCount": 25, "candidatesTokenCount": 50, "thoughtsTokenCount": 100},
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.backend_ai.gateway.providers.gemini.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    install_provider_builder(monkeypatch, module, GeminiProvider(api_key="test-key"))
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "gateway_request_id": routing["request_id"],
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "quality_evaluation": True,
            "evaluator_model_id": model_id,
            "idempotency_key": "native-gemini",
        },
    )
    assert created.status_code == 202, created.text
    comparison_id = created.json()["comparison_id"]
    async with factory() as session:
        service = ComparisonExecutionService(session, settings)
        await service.claim_next()
        await service.process(comparison_id)
    report = (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()
    assert report["status"] == "completed"
    assert report["evaluation_status"] == "completed"
    assert report["evaluation_total_tokens"] == 175
    assert [branch["quality_score"] for branch in report["branches"]] == [80, 80]
    assert len(calls) == 2 and "responseFormat" not in calls[0]


@pytest.mark.asyncio
async def test_empty_reference_output_keeps_observed_cost(comparison_client, monkeypatch):
    from src.backend_ai.gateway.providers.base import ProviderClient, ProviderOutputError, ProviderResponse
    from src.backend_ai.services import comparison_service as module

    client, factory, settings, application_id, model_id = comparison_client
    routing = await saved_routing(factory, settings, application_id)

    class EmptyProvider(ProviderClient):
        async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
            raise ProviderOutputError(
                "OUTPUT_TOKEN_LIMIT",
                "No text",
                ProviderResponse(
                    text="",
                    provider="offline",
                    model_id=model_id,
                    input_tokens=10,
                    output_tokens=800,
                    usage_received=True,
                ),
            )

    install_provider_builder(monkeypatch, module, EmptyProvider())
    created = await client.post(
        "/api/v1/gateway/comparisons",
        json={
            "application_id": application_id,
            "gateway_request_id": routing["request_id"],
            "prompt": "Say hello",
            "reference_model_ids": [model_id],
            "idempotency_key": "empty-reference",
        },
    )
    assert created.status_code == 202, created.text
    comparison_id = created.json()["comparison_id"]
    async with factory() as session:
        service = ComparisonExecutionService(session, settings)
        await service.claim_next()
        await service.process(comparison_id)
    report = (await client.get(f"/api/v1/gateway/comparisons/{comparison_id}")).json()
    assert report["status"] == "partial"
    reference = next(branch for branch in report["branches"] if branch["branch_type"] == "reference")
    assert reference["status"] == "failed"
    assert reference["error_code"] == "OUTPUT_TOKEN_LIMIT"
    assert reference["total_tokens"] == 810
    assert reference["usage_complete"] is True
    assert reference["cost_usd"] > 0
    assert report["deltas"][0]["comparable_cost"] is False
