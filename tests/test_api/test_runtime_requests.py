import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.shared.models  # noqa: F401
from src.backend.dtos.application_keys import ApplicationKeyCreate
from src.backend.dtos.applications import ApplicationCreate
from src.backend.dtos.catalog import ModelCreate, ModelPoolCreate, ModelPoolMemberCreate, PolicyCreate, ProviderCreate
from src.backend.dtos.gateway_configuration import GatewayConfigUpsert
from src.backend.services.application_key_service import ApplicationKeyService
from src.backend.services.application_service import ApplicationService
from src.backend.services.catalog_service import CatalogService
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.backend_ai.services.comparison_service import ComparisonExecutionService
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.gateway import ApplicationGatewayConfig, Model
from src.shared.models.runtime import GatewayRequestRecord, RuntimeOperationRecord, UsageEventRecord
from tests.service_app import app


@pytest.mark.asyncio
async def test_runtime_execution_uses_normalized_fingerprint_before_config_preflight(monkeypatch):
    from starlette.requests import Request

    from src.backend_ai.routers import runtime as runtime_module
    from src.backend_ai.services.application_auth_service import ApplicationPrincipal
    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint
    from src.backend_ai.services.runtime_operation_service import ClaimResult
    from src.shared.dtos.runtime_api import RuntimeRequestInput

    fingerprint = build_openai_fingerprint("openai_responses", {"model": "m", "input": "hello"})
    operation = SimpleNamespace(
        request_id="operation-v2",
        public_result_json={"request_id": "operation-v2", "status": "completed"},
        http_body_json=None,
        http_status=200,
    )
    calls = []

    class Operations:
        def __init__(self, session, **kwargs):
            pass

        async def lookup_normalized(self, principal, key, actual_fingerprint):
            calls.append((principal.application_id, key, actual_fingerprint))
            return ClaimResult("replay", operation)

    monkeypatch.setattr(runtime_module, "RuntimeOperationService", Operations)
    monkeypatch.setattr(
        runtime_module,
        "get_settings",
        lambda: SimpleNamespace(
            gateway_runtime_requests_per_minute=60,
            gateway_runtime_max_concurrent_requests=4,
            gateway_runtime_deadline_seconds=120,
        ),
    )
    request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": []})

    response = await runtime_module.execute_runtime_request(
        request,
        ApplicationPrincipal(application_id="app-test", key_id="key-test"),
        None,
        RuntimeRequestInput(prompt="hello"),
        idempotency_key="response-key",
        request_id="request-new",
        operation_fingerprint=fingerprint,
    )

    assert calls == [("app-test", "response-key", fingerprint)]
    assert response.status_code == 200
    assert response.headers["idempotency-replayed"] == "true"
    assert response.headers["x-request-id"] == "operation-v2"


@pytest.mark.asyncio
async def test_output_policy_failure_is_finalized_failed_before_billing(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from starlette.requests import Request

    from src.backend_ai.routers import runtime as runtime_module
    from src.backend_ai.services.application_auth_service import ApplicationPrincipal
    from src.backend_ai.services.openai_operation_fingerprint import OperationFingerprint
    from src.backend_ai.services.runtime_operation_service import ClaimResult
    from src.shared.dtos.gateway import (
        AggregationResult,
        AggregationStatus,
        CostSummary,
        GatewayRequest,
        PipelineResult,
        ValidationResult,
    )
    from src.shared.dtos.runtime_api import (
        RuntimeAPIError,
        RuntimeOperationError,
        RuntimeOutputPolicy,
        RuntimeRequestInput,
    )

    operation = SimpleNamespace(
        request_id="policy-operation",
        started_at=datetime(2026, 10, 7, tzinfo=UTC),
        execution_mode="live",
    )
    pipeline = PipelineResult.model_construct(
        request=GatewayRequest.model_construct(
            request_id="policy-operation", application_id="app", metadata={}
        ),
        validation=ValidationResult.model_construct(accepted=True, errors=[]),
        execution_results=[],
        aggregation=AggregationResult(
            original_request_id="policy-operation", status=AggregationStatus.COMPLETE,
            final_answer="[1]", strategy="none", reason="fixture",
        ),
        traces=[],
        costs=CostSummary(),
    )
    staged = {}

    class Operations:
        def __init__(self, session, **kwargs):
            self.session = session

        async def lookup_normalized(self, *args, **kwargs):
            return None

        async def claim_normalized(self, *args, **kwargs):
            return ClaimResult("owner", operation)

        async def finalize_in_transaction(self, request_id, **kwargs):
            staged["finalize"] = (request_id, kwargs)
            return True

    class Execution:
        def __init__(self, session, settings):
            pass

        async def prepare_public(self, principal, payload):
            return SimpleNamespace(config=SimpleNamespace(version=1), snapshot_json={})

        async def run_public(self, *args):
            return pipeline

    class Persistence:
        def __init__(self, session):
            pass

        async def stage_run(self, result, **kwargs):
            staged["persistence"] = kwargs

    class Session:
        async def commit(self):
            staged["committed"] = True

        async def rollback(self):
            staged["rolled_back"] = True

    monkeypatch.setattr(runtime_module, "RuntimeOperationService", Operations)
    monkeypatch.setattr(runtime_module, "GatewayExecutionService", Execution)
    monkeypatch.setattr(runtime_module, "GatewayPersistenceService", Persistence)
    monkeypatch.setattr(runtime_module, "get_settings", lambda: SimpleNamespace(
        gateway_runtime_requests_per_minute=60, gateway_runtime_max_concurrent_requests=4,
        gateway_runtime_deadline_seconds=10, gateway_execution_mode="live",
    ))
    request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [], "state": {}})
    policy_error = RuntimeOperationError(
        code="INVALID_PROVIDER_RESPONSE", message="not JSON object", details={"param": "text.format"}
    )

    with pytest.raises(RuntimeAPIError) as error:
        await runtime_module.execute_runtime_request(
            request,
            ApplicationPrincipal("app", "key"),
            Session(),
            RuntimeRequestInput(prompt="hello"),
            idempotency_key="policy-key",
            request_id="request-new",
            operation_fingerprint=OperationFingerprint("f" * 64, 2),
            output_policy=lambda result: RuntimeOutputPolicy(error=policy_error),
        )

    assert error.value.status_code == 502
    assert staged["persistence"]["status_override"] == "failed"
    assert staged["finalize"][1]["http_status"] == 502
    assert staged["finalize"][1]["public_result"]["status"] == "failed"
    assert staged["finalize"][1]["http_body"]["error"]["details"]["param"] == "text.format"
    assert staged["committed"] is True


@pytest_asyncio.fixture
async def runtime_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.backend_ai.routers import runtime as runtime_module

    settings = Settings(
        _env_file=None,
        gateway_execution_mode="offline",
        gateway_runtime_enabled=True,
        gateway_runtime_deadline_seconds=5,
        gateway_runtime_requests_per_minute=60,
        gateway_runtime_max_concurrent_requests=4,
    )
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "native-runtime.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        application = await ApplicationService(session).create(
            "owner-1", ApplicationCreate(slug="native-runtime", name="Native runtime")
        )
        catalog = CatalogService(session)
        provider = await catalog.create_provider(ProviderCreate(name="local", display_name="Local", kind="offline"))
        model = await catalog.create_model(
            ModelCreate(
                provider_id=provider.id,
                name="local-deterministic",
                display_name="Local deterministic",
                capabilities=["chat", "text"],
            )
        )
        pool = await catalog.create_pool(
            ModelPoolCreate(
                name="native-runtime-pool",
                purpose="runtime",
                members=[ModelPoolMemberCreate(model_id=model.id)],
            ),
            "owner-1",
        )
        policy = await catalog.create_policy(PolicyCreate(name="native-runtime-policy", fallback_chain=[model.id]))
        await GatewayConfigurationService(session).upsert(
            "owner-1",
            application.id,
            GatewayConfigUpsert(
                model_pool_id=pool.id,
                routing_policy_id=policy.id,
                routing_objective="cost_first",
                default_router="deterministic",
                default_aggregator="none",
            ),
        )
        key = await ApplicationKeyService(session).create(
            "owner-1", application.id, ApplicationKeyCreate(name="Runtime API key")
        )

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="owner-1",
            email="owner@example.com",
            roles=("owner",),
            permissions=frozenset({"applications.read_own", "applications.manage_own"}),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, application.id, key.raw_key, key.id, factory, settings
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_native_runtime_requires_gateway_key_and_feature_flag(runtime_client):
    client, _, _, _, _, settings = runtime_client
    missing = await client.post("/api/v1/runtime/requests", json={"prompt": "hello"})
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AUTHENTICATION_FAILED"
    assert missing.headers["www-authenticate"] == "Bearer"

    settings.gateway_runtime_enabled = False
    disabled = await client.post(
        "/api/v1/runtime/requests", headers={"Authorization": "Bearer ak_" + "x" * 40}, json={"prompt": "hello"}
    )
    assert disabled.status_code == 503
    assert disabled.json()["error"]["code"] == "RUNTIME_DISABLED"


@pytest.mark.asyncio
async def test_runtime_post_get_replay_and_revoke(runtime_client):
    client, application_id, raw_key, key_id, factory, _ = runtime_client
    headers = {"Authorization": f"Bearer {raw_key}", "Idempotency-Key": "ticket-123-v1"}
    created = await client.post("/api/v1/runtime/requests", headers=headers, json={"prompt": "Say hello"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["application_id"] == application_id
    assert body["status"] == "completed"
    assert body["output"]
    assert body["execution_mode"] == "offline"
    assert created.headers["x-request-id"] == body["request_id"]
    assert created.headers["x-gateway-api-version"] == "1"
    assert created.headers["idempotency-replayed"] == "false"

    detail = await client.get(f"/api/v1/runtime/requests/{body['request_id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json() == body

    duplicate = await client.post("/api/v1/runtime/requests", headers=headers, json={"prompt": "Say hello"})
    assert duplicate.status_code == 200
    assert duplicate.json() == body
    assert duplicate.headers["idempotency-replayed"] == "true"

    conflict = await client.post("/api/v1/runtime/requests", headers=headers, json={"prompt": "Different"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    async with factory() as session:
        requests = await session.scalar(select(func.count()).select_from(GatewayRequestRecord))
        operations = await session.scalar(select(func.count()).select_from(RuntimeOperationRecord))
        usage = await session.scalar(select(func.count()).select_from(UsageEventRecord))
        operation = await session.get(RuntimeOperationRecord, body["request_id"])
        assert operation is not None and operation.http_body_json == body
        snapshot = operation.config_snapshot_json
        await ApplicationKeyService(session).revoke("owner-1", application_id, key_id)
    assert requests == operations == 1
    assert usage == 1
    assert snapshot["pool"]["name"] == "native-runtime-pool"
    assert "routing_strategy" not in snapshot["pool"]
    assert snapshot["policy"]["name"] == "native-runtime-policy"
    assert snapshot["policy"]["priority"] == 100
    assert snapshot["policy"]["quality_scores"] == {}
    assert "pool_priority" not in snapshot["models"][0]
    assert snapshot["models"][0]["quality_tier"] == "standard"
    assert snapshot["models"][0]["target_latency_ms"] is None
    assert snapshot["models"][0]["context_window"] is None

    revoked = await client.get(f"/api/v1/runtime/requests/{body['request_id']}", headers=headers)
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "AUTHENTICATION_FAILED"


@pytest.mark.asyncio
async def test_runtime_comparison_reuses_gateway_receipt_and_exposes_report(runtime_client):
    client, _, raw_key, _, factory, settings = runtime_client
    async with factory() as session:
        model_id = await session.scalar(select(Model.id).where(Model.name == "local-deterministic"))
    headers = {"Authorization": f"Bearer {raw_key}", "Idempotency-Key": "runtime-comparison"}
    created = await client.post(
        "/api/v1/runtime/requests",
        headers=headers,
        json={
            "prompt": "Say hello once",
            "comparison": {"reference_model_ids": [model_id]},
        },
    )
    assert created.status_code == 200, created.text
    comparison_id = created.json()["comparison_id"]
    assert comparison_id
    replayed = await client.post(
        "/api/v1/runtime/requests",
        headers=headers,
        json={
            "prompt": "Say hello once",
            "comparison": {"reference_model_ids": [model_id]},
        },
    )
    assert replayed.status_code == 200
    assert replayed.json()["comparison_id"] == comparison_id
    assert replayed.headers["idempotency-replayed"] == "true"

    async with factory() as session:
        service = ComparisonExecutionService(session, settings)
        assert await service.claim_next() == comparison_id
    async with factory() as session:
        await ComparisonExecutionService(session, settings).process(comparison_id)

    report = await client.get(f"/api/v1/runtime/comparisons/{comparison_id}", headers=headers)
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["gateway_request_id"] == created.json()["request_id"]
    assert body["status"] == "completed"
    assert len(body["branches"]) == 2
    async with factory() as session:
        request_count = await session.scalar(select(func.count()).select_from(GatewayRequestRecord))
    assert request_count == 1


@pytest.mark.asyncio
async def test_offline_pipeline_runs_the_classifier_for_a_jev_application(runtime_client):
    # New applications default to jev. The offline pipeline calls no external service, so it runs
    # the classifier understander instead (rules here, no model in tests) and says so in a warning.
    client, _, raw_key, _, factory, _ = runtime_client
    response = await client.post(
        "/api/v1/runtime/requests", headers={"Authorization": f"Bearer {raw_key}"}, json={"prompt": "Say hello"}
    )
    assert response.status_code == 200, response.text
    warning = next(item for item in response.json()["warnings"] if item["code"] == "PIPELINE_STRATEGY_SUBSTITUTED")
    assert warning["details"]["understander"] == {"requested": "jev", "effective": "classifier"}
    async with factory() as session:
        config = await session.scalar(select(ApplicationGatewayConfig))
        record = await session.get(GatewayRequestRecord, response.json()["request_id"])
    assert config.default_understander == "jev"
    assert "classifier->rule" in json.dumps(record.response_json)
    assert "jev-placeholder" not in json.dumps(record.response_json)


@pytest.mark.asyncio
async def test_runtime_payload_cannot_choose_application_or_execution_strategy(runtime_client):
    client, _, raw_key, _, _, _ = runtime_client
    headers = {"Authorization": f"Bearer {raw_key}"}
    for payload in (
        {"prompt": "hello", "application_id": "someone-elses-app"},
        {"prompt": "hello", "router": "deterministic"},
        {"prompt": "hello", "source": "playground"},
    ):
        response = await client.post("/api/v1/runtime/requests", headers=headers, json=payload)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_runtime_uses_configured_defaults_and_forwards_explicit_generation_cap(runtime_client, monkeypatch):
    client, application_id, raw_key, _, factory, _ = runtime_client
    from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse
    from src.backend_ai.services import gateway_execution_service

    class CaptureProvider(ProviderClient):
        name = "offline"

        def __init__(self):
            self.caps = []

        async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
            self.caps.append(options.max_output_tokens if options else None)
            return ProviderResponse(
                text="captured response",
                provider=self.name,
                model_id=model_id,
                input_tokens=3,
                output_tokens=2,
                usage_received=True,
            )

    provider = CaptureProvider()
    class CaptureProviderBuilder:
        def __init__(self, resolver, settings):
            self.available_provider_ids = set()

        async def build_for_models(self, rows):
            output = {}
            for model, connection in rows:
                self.available_provider_ids.add(connection.id)
                output[model.id] = provider
                output[connection.name] = provider
            return output

    monkeypatch.setattr(gateway_execution_service, "ProviderClientBuilder", CaptureProviderBuilder)
    headers = {"Authorization": f"Bearer {raw_key}"}
    explicit = await client.post(
        "/api/v1/runtime/requests",
        headers={**headers, "Idempotency-Key": "explicit-cap"},
        json={"prompt": "Say hello", "constraints": {"quality_priority": "balanced", "max_output_tokens": 41}},
    )
    defaulted = await client.post(
        "/api/v1/runtime/requests",
        headers={**headers, "Idempotency-Key": "application-defaults"},
        json={"prompt": "Say hello"},
    )
    assert explicit.status_code == defaulted.status_code == 200
    assert explicit.json()["warnings"][0]["code"] == "ROUTER_FORCED_DATABASE"
    assert provider.caps == [41, 800]
    async with factory() as session:
        first = await session.get(GatewayRequestRecord, explicit.json()["request_id"])
        second = await session.get(GatewayRequestRecord, defaulted.json()["request_id"])
    assert first.response_json["effective_constraints"]["constraints"]["quality_priority"] == "balanced"
    assert first.response_json["effective_constraints"]["constraints"]["max_output_tokens"] == 41
    assert second.response_json["effective_constraints"]["constraints"]["quality_priority"] == "cost_first"


@pytest.mark.asyncio
async def test_runtime_rejects_oversized_and_credential_bearing_bodies_before_claim(runtime_client):
    client, _, raw_key, _, factory, _ = runtime_client
    headers = {"Authorization": f"Bearer {raw_key}"}
    too_large = await client.post(
        "/api/v1/runtime/requests",
        headers=headers,
        content=b'{"prompt":"' + b"x" * (256 * 1024) + b'"}',
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "REQUEST_TOO_LARGE"

    oversized_context = await client.post(
        "/api/v1/runtime/requests",
        headers=headers,
        json={"prompt": "hello", "context": {"text": "x" * (64 * 1024)}},
    )
    assert oversized_context.status_code == 422

    credential = await client.post(
        "/api/v1/runtime/requests",
        headers=headers,
        json={"prompt": "hello", "metadata": {"note": "ak_" + "s" * 40}},
    )
    assert credential.status_code == 422
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(RuntimeOperationRecord))
    assert count == 0
