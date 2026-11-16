import base64
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
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
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dtos.runtime_api import RuntimeRequestResponse, RuntimeUsage, RuntimeUsageScope
from src.shared.models.billing import BillingTransactionRecord
from src.shared.models.runtime import GatewayRequestRecord, GatewayTaskRecord, UsageEventRecord
from tests.service_app import app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MULTI = "Thực hiện 2 task sau:\n\nTính 27 × 14.\n\nDịch câu \"xin chào\" sang tiếng Anh."


@pytest_asyncio.fixture
async def openai_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One application with an offline model pool and two keys (active +
    revoked); the same settings are patched into every module that reads them."""
    from src.backend_ai.dependencies import openai_auth
    from src.backend_ai.routers import openai_compat, openai_files, openai_responses, runtime
    from src.backend_ai.services import openai_model_service

    settings = Settings(
        gateway_openai_idempotency_policy="best_effort",
        _env_file=None,
        gateway_execution_mode="offline",
        gateway_runtime_enabled=True,
        gateway_runtime_deadline_seconds=10,
        gateway_runtime_requests_per_minute=120,
        gateway_runtime_max_concurrent_requests=8,
        gateway_files_storage="local",
        gateway_files_local_dir=str(tmp_path / "blobs"),
        gateway_files_max_bytes=200_000,
        # Offline has no jev split gate; legacy keeps the multi-task split these tests rely on.
        gateway_decomposition_mode="legacy",
    )
    for module in (runtime, openai_compat, openai_files, openai_responses, openai_auth, openai_model_service):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    isolated_pg = os.getenv("OPENAI_AUDIT_CONTRACT_PG_URL")
    schema = "sdk_contract_" + uuid4().hex if isolated_pg else None
    if isolated_pg:
        assert isolated_pg.startswith("postgresql+"), "Explicit isolated PostgreSQL URL required"
        admin_engine = create_async_engine(isolated_pg)
        async with admin_engine.begin() as connection:
            await connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        engine = create_async_engine(isolated_pg, connect_args={"options": "-csearch_path=" + schema},
                                     pool_size=10, max_overflow=0)
    else:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "openai.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        apps = ApplicationService(session)
        application = await apps.create("owner-1", ApplicationCreate(slug="openai-sdk", name="OpenAI SDK"))
        other = await apps.create("owner-1", ApplicationCreate(slug="openai-other", name="Other"))
        catalog = CatalogService(session)
        provider = await catalog.create_provider(ProviderCreate(name="local", display_name="Local", kind="offline"))
        model = await catalog.create_model(
            ModelCreate(
                provider_id=provider.id,
                name="local-deterministic",
                display_name="Local deterministic",
                capabilities=["chat", "text", "structured_output"],
            )
        )
        for app_record, suffix in ((application, "a"), (other, "b")):
            pool = await catalog.create_pool(
                ModelPoolCreate(
                    name=f"openai-pool-{suffix}", purpose="runtime", members=[ModelPoolMemberCreate(model_id=model.id)]
                ),
                "owner-1",
            )
            policy = await catalog.create_policy(PolicyCreate(name=f"openai-policy-{suffix}", fallback_chain=[model.id]))
            await GatewayConfigurationService(session).upsert(
                "owner-1",
                app_record.id,
                GatewayConfigUpsert(
                    model_pool_id=pool.id,
                    routing_policy_id=policy.id,
                    default_decomposer="deterministic",
                    default_aggregator="deterministic",
                ),
            )
        keys = ApplicationKeyService(session)
        active = await keys.create("owner-1", application.id, ApplicationKeyCreate(name="sdk"))
        revoked = await keys.create("owner-1", application.id, ApplicationKeyCreate(name="old"))
        await keys.revoke("owner-1", application.id, revoked.id)
        other_key = await keys.create("owner-1", other.id, ApplicationKeyCreate(name="other"))

    async def override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {
            "client": client,
            "key": active.raw_key,
            "revoked": revoked.raw_key,
            "other_key": other_key.raw_key,
            "factory": factory,
            "settings": settings,
            "tmp_path": tmp_path,
        }
    app.dependency_overrides.clear()
    await engine.dispose()
    if isolated_pg:
        async with admin_engine.begin() as connection:
            await connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        await admin_engine.dispose()


def _auth(key, **extra):
    return {"Authorization": f"Bearer {key}", **extra}


def _chat(content="Say hello", model="auto", **extra):
    return {"model": model, "messages": [{"role": "user", "content": content}], **extra}


def _usage_result(
    *, execution: tuple[int, int] | None, pipeline: tuple[int, int] | None, include_usage: bool = True
) -> RuntimeRequestResponse:
    usage = None
    if include_usage:
        execution_scope = None
        if execution is not None:
            execution_input, execution_output = execution
            execution_scope = RuntimeUsageScope(
                input_tokens=execution_input,
                output_tokens=execution_output,
                total_tokens=execution_input + execution_output,
            )
        pipeline_scope = None
        if pipeline is not None:
            pipeline_input, pipeline_output = pipeline
            pipeline_scope = RuntimeUsageScope(
                input_tokens=pipeline_input,
                output_tokens=pipeline_output,
                total_tokens=pipeline_input + pipeline_output,
            )
        usage = RuntimeUsage(
            execution=execution_scope,
            pipeline=pipeline_scope,
            complete=True,
        )
    return RuntimeRequestResponse(
        request_id="usage-test",
        application_id="app-test",
        status="completed",
        execution_mode="live",
        config_version=1,
        started_at=datetime(2026, 10, 7, tzinfo=UTC),
        usage=usage,
    )


def test_chat_usage_uses_inclusive_pipeline_once():
    from src.backend_ai.routers.openai_compat import _usage

    usage = _usage(_usage_result(execution=(49, 5), pipeline=(49, 5)))

    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (49, 5, 54)


def test_chat_usage_excludes_internal_calls_from_public_completion():
    from src.backend_ai.routers.openai_compat import _usage

    usage = _usage(_usage_result(execution=(49, 5), pipeline=(56, 7)))

    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (49, 5, 54)


def test_chat_usage_falls_back_to_execution_when_pipeline_is_absent():
    from src.backend_ai.routers.openai_compat import _usage

    usage = _usage(_usage_result(execution=(49, 5), pipeline=None))

    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (49, 5, 54)


def test_chat_usage_remains_unknown_when_usage_is_absent():
    from src.backend_ai.routers.openai_compat import _usage

    usage = _usage(_usage_result(execution=None, pipeline=None, include_usage=False))

    assert usage is None


def test_runtime_error_response_mapping():
    from starlette.responses import JSONResponse

    from src.backend_ai.routers.openai_errors import from_runtime_response

    response = JSONResponse(
        status_code=502,
        content={
            "error": {
                "code": "PROVIDER_EXECUTION_FAILED",
                "message": "The request could not be completed.",
                "retryable": False,
                "details": {"param": "model"},
            },
            "request_id": "runtime-error-id",
        },
    )

    error = from_runtime_response(response)

    assert error.status_code == 502
    assert error.body() == {
        "error": {
            "message": "The request could not be completed.",
            "type": "server_error",
            "param": "model",
            "code": "provider_execution_failed",
        }
    }
    assert error.headers["x-request-id"] == "runtime-error-id"
    assert error.headers["x-gateway-request-id"] == "runtime-error-id"


def test_pinned_model_error_mapping_keeps_runtime_request_ids():
    from src.backend_ai.routers.openai_errors import from_runtime_error
    from src.shared.dtos.runtime_api import RuntimeAPIError

    error = from_runtime_error(
        RuntimeAPIError(422, "NO_ELIGIBLE_MODEL", "No candidate.", request_id="pinned-error-id"),
        pinned_model="missing-model",
    )

    assert error.status_code == 404
    assert error.body()["error"]["code"] == "model_not_found"
    assert error.headers["x-request-id"] == "pinned-error-id"
    assert error.headers["x-gateway-request-id"] == "pinned-error-id"


def test_malformed_stored_error_returns_json_error():
    from starlette.responses import JSONResponse

    from src.backend_ai.routers.openai_errors import from_runtime_response

    response = JSONResponse(
        status_code=502,
        content={"credentials": "must not escape"},
        headers={"x-request-id": "stored-id", "retry-after": "3"},
    )

    error = from_runtime_response(response)

    assert error.status_code == 502
    assert error.body()["error"]["code"] == "invalid_runtime_response"
    assert error.body()["error"]["type"] == "server_error"
    assert "credentials" not in str(error.body())
    assert error.headers["x-request-id"] == "stored-id"
    assert error.headers["retry-after"] == "3"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_terminal_error_replay_preserves_openai_contract(openai_env, monkeypatch, stream):
    from openai import APIStatusError, AsyncOpenAI

    from src.backend_ai.gateway.providers.base import PermanentProviderError
    from src.backend_ai.gateway.providers.offline import OfflineProvider

    calls = 0

    async def fail_generation(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        nonlocal calls
        calls += 1
        raise PermanentProviderError("test provider failure", status_code=400)

    monkeypatch.setattr(OfflineProvider, "generate", fail_generation)
    transport = httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )
    sdk = AsyncOpenAI(base_url="http://test/v1", api_key=openai_env["key"], http_client=transport, max_retries=0)
    key = f"terminal-replay-{stream}"
    headers = {"Idempotency-Key": key}
    body = _chat(stream=stream)

    with pytest.raises(APIStatusError) as first_error:
        await sdk.chat.completions.create(**body, extra_headers=headers)
    first = first_error.value
    assert first.status_code == 502
    assert first.response.json()["error"]["code"] == "provider_execution_failed"
    assert first.response.headers["content-type"].startswith("application/json")

    with pytest.raises(APIStatusError) as replay_error:
        await sdk.chat.completions.create(**body, extra_headers=headers)
    replay = replay_error.value
    assert replay.status_code == 502
    assert replay.response.json() == first.response.json()
    assert first.request_id == first.response.headers["x-gateway-request-id"]
    assert first.response.headers["idempotency-replayed"] == "false"
    assert first.response.headers["idempotency-key"] == key
    assert replay.request_id == first.request_id
    assert replay.response.headers["content-type"].startswith("application/json")
    assert replay.response.headers["x-gateway-request-id"] == first.request_id
    assert replay.response.headers["idempotency-replayed"] == "true"
    assert replay.response.headers["idempotency-key"] == key

    async with openai_env["factory"]() as session:
        assert await session.scalar(select(func.count()).select_from(GatewayRequestRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(BillingTransactionRecord)) == 0
    assert calls == 1
    await sdk.close()


@pytest.mark.asyncio
async def test_auth_errors_use_the_openai_envelope(openai_env):
    client = openai_env["client"]
    for headers in ({}, _auth("ak_" + "x" * 40), _auth(openai_env["revoked"])):
        response = await client.post("/v1/chat/completions", json=_chat(), headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"
    openai_env["settings"].gateway_runtime_enabled = False
    disabled = await client.post("/v1/chat/completions", json=_chat(), headers=_auth(openai_env["key"]))
    assert disabled.status_code == 503 and disabled.json()["error"]["code"] == "runtime_disabled"


@pytest.mark.asyncio
async def test_auto_runs_through_the_runtime_with_receipt_billing_and_replay(openai_env):
    client, key = openai_env["client"], openai_env["key"]
    headers = _auth(key, **{"Idempotency-Key": "chat-1"})
    response = await client.post("/v1/chat/completions", json=_chat(), headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "local-deterministic"
    assert body["choices"][0]["message"]["content"].startswith("offline:")
    assert body["usage"]["total_tokens"] == body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    assert response.headers["x-gateway-status"] == "completed"
    assert response.headers["idempotency-replayed"] == "false"
    receipt_response = await client.get(f"/api/v1/runtime/requests/{body['id']}", headers=_auth(key))
    assert receipt_response.status_code == 200
    pipeline_usage = receipt_response.json()["usage"]["pipeline"]
    assert body["usage"] == {
        "prompt_tokens": pipeline_usage["input_tokens"],
        "completion_tokens": pipeline_usage["output_tokens"],
        "total_tokens": pipeline_usage["total_tokens"],
    }
    async with openai_env["factory"]() as session:
        debit_count_before_replay = await session.scalar(
            select(func.count()).select_from(BillingTransactionRecord).where(
                BillingTransactionRecord.source_request_id == body["id"]
            )
        )

    replay = await client.post("/v1/chat/completions", json=_chat(), headers=headers)
    assert replay.status_code == 200 and replay.json()["id"] == body["id"]
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["usage"] == body["usage"]
    async with openai_env["factory"]() as session:
        debit_count_after_replay = await session.scalar(
            select(func.count()).select_from(BillingTransactionRecord).where(
                BillingTransactionRecord.source_request_id == body["id"]
            )
        )
    assert debit_count_after_replay == debit_count_before_replay
    conflict = await client.post("/v1/chat/completions", json=_chat("Different"), headers=headers)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"

    async with openai_env["factory"]() as session:
        assert await session.scalar(select(func.count()).select_from(GatewayRequestRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(UsageEventRecord)) >= 1


@pytest.mark.asyncio
async def test_model_field_controls_routing(openai_env):
    client, key = openai_env["client"], openai_env["key"]
    pinned = await client.post("/v1/chat/completions", json=_chat(model="local-deterministic"), headers=_auth(key))
    assert pinned.status_code == 200, pinned.text
    unknown = await client.post("/v1/chat/completions", json=_chat(model="gpt-9"), headers=_auth(key))
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "model_not_found"

    split = await client.post("/v1/chat/completions", json=_chat(MULTI), headers=_auth(key))
    fast = await client.post("/v1/chat/completions", json=_chat(MULTI, model="auto:fast"), headers=_auth(key))
    assert split.status_code == fast.status_code == 200
    async with openai_env["factory"]() as session:
        async def tasks(request_id):
            return await session.scalar(
                select(func.count()).select_from(GatewayTaskRecord).where(GatewayTaskRecord.request_id == request_id)
            )

        assert await tasks(split.json()["id"]) == 2
        assert await tasks(fast.json()["id"]) == 1  # latency_first skips decomposition


@pytest.mark.asyncio
async def test_auto_code_alias_routes_on_the_code_template(openai_env):
    """The application's pool has no 3-tier ladder, so auto:code uses the system
    coding template; plain auto (no default pool here) keeps the application's pool."""
    from src.shared.models.gateway import Model, ModelPool, ModelPoolMember, Provider
    from src.shared.repositories.routing_catalog import system_pool_purpose

    client, key = openai_env["client"], openai_env["key"]
    async with openai_env["factory"]() as session:
        provider = await session.scalar(select(Provider).where(Provider.name == "local"))
        pool = ModelPool(owner_user_id=None, name="Code", purpose=system_pool_purpose("code"), status="active")
        session.add(pool)
        await session.flush()
        for name, tier in (("code-small", "simple"), ("code-mid", "mid"), ("code-large", "hard")):
            model = Model(provider_id=provider.id, name=name, display_name=name, status="active", quality_tier=tier,
                          capabilities_json=["chat", "text", "structured_output"])
            session.add(model)
            await session.flush()
            session.add(ModelPoolMember(pool_id=pool.id, model_id=model.id, role="primary"))
        await session.commit()

    code = await client.post("/v1/chat/completions", json=_chat(model="auto:code"), headers=_auth(key))
    assert code.status_code == 200, code.text
    assert code.json()["model"].startswith("code-")
    plain = await client.post("/v1/chat/completions", json=_chat(model="auto"), headers=_auth(key))
    assert plain.status_code == 200 and plain.json()["model"] == "local-deterministic"
    models = await client.get("/v1/models", headers=_auth(key))
    assert "auto:code" in {card["id"] for card in models.json()["data"]}


@pytest.mark.asyncio
async def test_unsupported_and_invalid_requests(openai_env):
    client, key = openai_env["client"], openai_env["key"]
    tools = await client.post(
        "/v1/chat/completions", json=_chat(tools=[{"type": "web_search"}]), headers=_auth(key)
    )
    assert tools.status_code == 400 and tools.json()["error"]["param"] == "tools"
    invalid = await client.post("/v1/chat/completions", json={"model": "auto"}, headers=_auth(key))
    assert invalid.status_code == 400 and invalid.json()["error"]["param"] == "messages"
    unknown_option = await client.post(
        "/v1/chat/completions", json=_chat(gateway={"max_cost_usd": 1}), headers=_auth(key)
    )
    assert unknown_option.status_code == 400


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_unsupported_schema_feature_rejected_before_runtime_claim(openai_env, monkeypatch, strict, stream):
    from src.backend_ai.gateway.providers.offline import OfflineProvider

    provider_calls = 0

    async def count_generation(self, *args, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("unsupported response_format must fail before provider execution")

    monkeypatch.setattr(OfflineProvider, "generate", count_generation)
    schema_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "Answer",
            "strict": strict,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
                "patternProperties": {"^x": {"type": "string"}},
            },
        },
    }
    async with openai_env["factory"]() as session:
        requests_before = await session.scalar(select(func.count()).select_from(GatewayRequestRecord))
        debits_before = await session.scalar(select(func.count()).select_from(BillingTransactionRecord))

    response = await openai_env["client"].post(
        "/v1/chat/completions",
        json=_chat(response_format=schema_format, stream=stream),
        headers=_auth(openai_env["key"]),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert response.json()["error"]["param"] == "response_format"
    async with openai_env["factory"]() as session:
        assert await session.scalar(select(func.count()).select_from(GatewayRequestRecord)) == requests_before
        assert await session.scalar(select(func.count()).select_from(BillingTransactionRecord)) == debits_before
    assert provider_calls == 0


@pytest.mark.asyncio
async def test_sdk_parse_rejects_invalid_provider_output(openai_env):
    from openai import AsyncOpenAI, InternalServerError
    from pydantic import BaseModel

    class Answer(BaseModel):
        answer: str

    transport = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sdk = AsyncOpenAI(base_url="http://test/v1", api_key=openai_env["key"], http_client=transport, max_retries=0)

    with pytest.raises(InternalServerError) as error:
        await sdk.chat.completions.parse(model="auto", messages=[{"role": "user", "content": "answer"}], response_format=Answer)

    assert error.value.code == "invalid_provider_response"
    assert error.value.response.json()["error"]["param"] == "response_format"
    await sdk.close()


@pytest.mark.asyncio
async def test_supported_response_format_modes(openai_env, monkeypatch):
    from openai import AsyncOpenAI

    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.backend_ai.gateway.providers.offline import OfflineProvider
    from src.shared.dtos.gateway import OutputEvidence

    async def generate_json(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        return ProviderResponse(
            text='{"answer":"ok"}',
            provider=self.name,
            model_id=model_id,
            input_tokens=1,
            output_tokens=1,
            latency_ms=0.0,
            usage_received=True,
            output_evidence=OutputEvidence(finish_reason="stop"),
        )

    monkeypatch.setattr(OfflineProvider, "generate", generate_json)
    transport = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sdk = AsyncOpenAI(base_url="http://test/v1", api_key=openai_env["key"], http_client=transport, max_retries=0)

    text_response = await sdk.chat.completions.create(
        model="auto", messages=[{"role": "user", "content": "hello"}], response_format={"type": "text"}
    )
    json_response = await sdk.chat.completions.create(
        model="auto", messages=[{"role": "user", "content": "json"}], response_format={"type": "json_object"}
    )

    assert text_response.choices[0].message.content
    assert isinstance(json.loads(json_response.choices[0].message.content), dict)
    await sdk.close()


@pytest.mark.parametrize("response_format", [{}, {"type": "xml"}])
@pytest.mark.asyncio
async def test_unknown_or_missing_response_format_type_fails_before_claim(openai_env, response_format):
    async with openai_env["factory"]() as session:
        requests_before = await session.scalar(select(func.count()).select_from(GatewayRequestRecord))

    response = await openai_env["client"].post(
        "/v1/chat/completions", json=_chat(response_format=response_format), headers=_auth(openai_env["key"])
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["param"] == "response_format"
    async with openai_env["factory"]() as session:
        assert await session.scalar(select(func.count()).select_from(GatewayRequestRecord)) == requests_before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", -1),
        ("temperature", 99),
        ("temperature", float("nan")),
        ("temperature", float("inf")),
        ("temperature", True),
        ("temperature", "0.7"),
        ("top_p", -1),
        ("top_p", 2),
        ("top_p", float("nan")),
        ("top_p", float("-inf")),
        ("top_p", False),
        ("top_p", "0.5"),
        ("stop", []),
        ("stop", ["a", "b", "c", "d", "e"]),
        ("stop", [1]),
        ("stop", ""),
        ("stop", [""]),
    ],
)
@pytest.mark.asyncio
async def test_sampling_values_validate_before_execution(openai_env, monkeypatch, field, value):
    from src.backend_ai.gateway.providers.offline import OfflineProvider

    calls = 0

    async def count_generation(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid sampling must fail before provider execution")

    monkeypatch.setattr(OfflineProvider, "generate", count_generation)
    async with openai_env["factory"]() as session:
        requests_before = await session.scalar(select(func.count()).select_from(GatewayRequestRecord))
        debits_before = await session.scalar(select(func.count()).select_from(BillingTransactionRecord))

    request_body = _chat(**{field: value})
    if isinstance(value, float) and not math.isfinite(value):
        response = await openai_env["client"].post(
            "/v1/chat/completions",
            content=json.dumps(request_body, allow_nan=True),
            headers={"content-type": "application/json", **_auth(openai_env["key"])},
        )
    else:
        response = await openai_env["client"].post(
            "/v1/chat/completions", json=request_body, headers=_auth(openai_env["key"])
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["param"] == field
    async with openai_env["factory"]() as session:
        assert await session.scalar(select(func.count()).select_from(GatewayRequestRecord)) == requests_before
        assert await session.scalar(select(func.count()).select_from(BillingTransactionRecord)) == debits_before
    assert calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 0),
        ("temperature", 0.7),
        ("temperature", 2),
        ("top_p", 0),
        ("top_p", 0.5),
        ("top_p", 1),
        ("stop", "STOP"),
        ("stop", ["STOP"]),
    ],
)
@pytest.mark.asyncio
async def test_supported_sampling_is_forwarded(openai_env, monkeypatch, field, value):
    from src.backend_ai.gateway.providers.offline import OfflineProvider

    captured = []
    original = OfflineProvider.generate

    async def generate(self, prompt, **kwargs):
        captured.append(kwargs["options"])
        return await original(self, prompt, **kwargs)

    monkeypatch.setattr(OfflineProvider, "generate", generate)
    response = await openai_env["client"].post(
        "/v1/chat/completions", json=_chat(**{field: value}), headers=_auth(openai_env["key"])
    )
    assert response.status_code == 200, response.text
    assert len(captured) == 1
    expected = value
    assert getattr(captured[0], field) == expected


@pytest.mark.asyncio
async def test_null_sampling_and_absent_fields_work(openai_env):
    client, key = openai_env["client"], openai_env["key"]
    explicit_null = await client.post(
        "/v1/chat/completions",
        json=_chat(temperature=None, top_p=None, stop=None),
        headers=_auth(key),
    )
    omitted = await client.post("/v1/chat/completions", json=_chat(), headers=_auth(key))

    assert explicit_null.status_code == 200, explicit_null.text
    assert omitted.status_code == 200, omitted.text


@pytest.mark.asyncio
async def test_function_tools_and_multiple_choices(openai_env):
    client, key = openai_env["client"], openai_env["key"]
    tools = await client.post(
        "/v1/chat/completions", json=_chat(tools=[{"type": "function", "function": {"name": "f"}}]), headers=_auth(key)
    )
    multiple = await client.post("/v1/chat/completions", json=_chat(n=2), headers=_auth(key))
    too_many = await client.post("/v1/chat/completions", json=_chat(n=5), headers=_auth(key))
    assert tools.status_code == 200, tools.text
    assert multiple.status_code == 200, multiple.text
    assert [choice["index"] for choice in multiple.json()["choices"]] == [0, 1]
    assert too_many.status_code == 400 and too_many.json()["error"]["param"] == "n"


@pytest.mark.asyncio
async def test_explicit_partial_image_response_warns_and_never_persists_media(openai_env):
    client, key = openai_env["client"], openai_env["key"]
    image = "data:image/png;base64," + base64.b64encode(PNG).decode()
    content = [{"type": "text", "text": "Ảnh này là gì?"}, {"type": "image_url", "image_url": {"url": image}}]
    response = await client.post("/v1/chat/completions", json=_chat(content, gateway={"allow_partial_response": True}), headers=_auth(key))
    assert response.status_code == 200, response.text
    assert response.json()["gateway_warnings"]
    async with openai_env["factory"]() as session:
        record = await session.scalar(select(GatewayRequestRecord))
    stored = json.dumps(record.messages_json)
    assert "base64" not in stored and "inline data omitted" in stored


@pytest.mark.asyncio
async def test_stream_and_models(openai_env):
    client, key = openai_env["client"], openai_env["key"]
    streamed = await client.post(
        "/v1/chat/completions", json=_chat(stream=True, stream_options={"include_usage": True}), headers=_auth(key)
    )
    events = [line[6:] for line in streamed.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert chunks[0]["choices"][0]["delta"]["content"].startswith("offline:")
    assert chunks[2]["usage"]["total_tokens"] > 0
    receipt = await client.get(f"/api/v1/runtime/requests/{chunks[0]['id']}", headers=_auth(key))
    assert receipt.status_code == 200
    pipeline_usage = receipt.json()["usage"]["pipeline"]
    assert chunks[2]["usage"] == {
        "prompt_tokens": pipeline_usage["input_tokens"],
        "completion_tokens": pipeline_usage["output_tokens"],
        "total_tokens": pipeline_usage["total_tokens"],
    }

    models = await client.get("/v1/models", headers=_auth(key))
    ids = [item["id"] for item in models.json()["data"]]
    assert ids[:4] == ["auto", "auto:cheap", "auto:quality", "auto:fast"]
    assert "local-deterministic" in ids


@pytest.mark.asyncio
async def test_official_openai_sdk_works_end_to_end(openai_env):
    from openai import AsyncOpenAI, NotFoundError

    http_client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sdk = AsyncOpenAI(base_url="http://test/v1", api_key=openai_env["key"], http_client=http_client, max_retries=0)
    image = "data:image/png;base64," + base64.b64encode(PNG).decode()
    completion = await sdk.chat.completions.create(
        model="auto:cheap",
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": [
                {"type": "text", "text": "What does this screenshot show?"},
                {"type": "image_url", "image_url": {"url": image}},
            ]},
        ],
        max_completion_tokens=200,
        extra_body={"gateway": {"external_provider_allowed": False, "allow_partial_response": True}},
    )
    assert completion.choices[0].message.content.startswith("offline:")
    assert completion.model == "local-deterministic"

    stream = await sdk.chat.completions.create(model="auto", messages=[{"role": "user", "content": "hi"}], stream=True)
    text = "".join([chunk.choices[0].delta.content or "" async for chunk in stream if chunk.choices])
    assert text.startswith("offline:")

    with pytest.raises(NotFoundError) as error:
        await sdk.chat.completions.create(model="nope", messages=[{"role": "user", "content": "hi"}])
    assert error.value.code == "model_not_found"
    await http_client.aclose()
