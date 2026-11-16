from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import Request

from src.backend_ai.dtos.openai_embeddings import EmbeddingRequest
from src.backend_ai.gateway.providers.embeddings import EmbeddingProviderError, EmbeddingProviderResult
from src.backend_ai.services import embedding_execution_service as execution_module
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.embedding_validation import EMBEDDING_PROFILES
from src.backend_ai.services.runtime_operation_service import ClaimResult
from src.shared.config import Settings


class FakeSession:
    def __init__(self, config):
        self.config = config
        self.commits = 0
        self.rollbacks = 0
        self.fail_commits = 0

    async def scalar(self, _query):
        return self.config

    async def commit(self):
        self.commits += 1
        if self.fail_commits:
            self.fail_commits -= 1
            raise RuntimeError("commit failed")

    async def rollback(self):
        self.rollbacks += 1


class FakeOperations:
    latest = None
    next_lookup = None
    latest_key = None
    latest_app_id = None

    @staticmethod
    def validate_idempotency_key(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(not 0x21 <= ord(ch) <= 0x7E for ch in value):
            raise ValueError("invalid idempotency key")

    def __init__(self, session, **_kwargs):
        self.session = session
        self.operation = self.__class__.latest

    async def lookup_normalized(self, principal, key, fingerprint):
        if self.__class__.next_lookup is not None:
            return self.__class__.next_lookup
        if (
            self.operation is None
            or key != self.__class__.latest_key
            or principal.application_id != self.__class__.latest_app_id
        ):
            return None
        if (
            fingerprint.payload_hash != self.__class__.latest_fingerprint.payload_hash
            or fingerprint.normalization_version != self.__class__.latest_fingerprint.normalization_version
        ):
            return ClaimResult("conflict", self.operation)
        if self.operation.status == "completed":
            return ClaimResult("replay", self.operation)
        if self.operation.status == "failed":
            return ClaimResult("terminal_replay", self.operation)
        if self.operation.status == "indeterminate":
            return ClaimResult("unknown", self.operation)
        return None

    async def claim_normalized(self, principal, key, fingerprint, **kwargs):
        operation = SimpleNamespace(
            request_id="operation-1",
            application_id=principal.application_id,
            application_api_key_id=principal.key_id,
            config_version=kwargs["config_version"],
            config_snapshot_json=kwargs["config_snapshot"],
            execution_mode="live",
            status="running",
            started_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            http_status=None,
            http_body_json=None,
            public_result_json=None,
        )
        self.operation = operation
        self.__class__.latest = operation
        self.__class__.latest_key = key
        self.__class__.latest_app_id = principal.application_id
        self.__class__.latest_fingerprint = fingerprint
        return ClaimResult("owner", operation)

    async def finalize_in_transaction(self, request_id, *, status, public_result, http_status, http_body, now):
        self.operation.status = status
        self.operation.public_result_json = public_result
        self.operation.http_status = http_status
        self.operation.http_body_json = http_body
        return True

    async def finalize(self, request_id, *, status, public_result, http_status, http_body, now):
        self.operation.status = status
        self.operation.public_result_json = public_result
        self.operation.http_status = http_status
        self.operation.http_body_json = http_body
        await self.session.commit()
        return True


class FakePersistence:
    staged = []

    def __init__(self, _session):
        pass

    async def stage_run(self, *args, **kwargs):
        self.__class__.staged.append((args, kwargs))


def fake_request():
    return Request({"type": "http", "method": "POST", "path": "/v1/embeddings", "headers": [], "state": {}})


def harness(monkeypatch, provider_behavior=None):
    class Encoding:
        n_vocab = 200_000

        @staticmethod
        def encode(value, **_kwargs):
            return [0] * len(value)

    monkeypatch.setattr("src.backend_ai.services.embedding_validation._get_tokenizer", lambda _name: Encoding())
    model = SimpleNamespace(
        id="model-1",
        name="text-embedding-3-small",
        input_price_per_million=2.0,
        pricing_version="price-1",
        capabilities_json=["embedding"],
    )
    provider = SimpleNamespace(
        id="provider-1",
        name="openai",
        kind="openai",
        base_url=None,
        secret_env_var="TEST_EMBEDDING_PROVIDER_KEY",
    )
    config = SimpleNamespace(version=7, timeout_seconds=3.0, retry_limit=0)
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite://",
        gateway_runtime_enabled=True,
        gateway_execution_mode="live",
        gateway_runtime_deadline_seconds=5,
        gateway_runtime_requests_per_minute=120,
        gateway_runtime_max_concurrent_requests=8,
        gateway_embeddings_max_batch_size=128,
        gateway_openai_enabled=True,
    )
    FakeOperations.latest = None
    FakeOperations.next_lookup = None
    FakeOperations.latest_key = None
    FakeOperations.latest_app_id = None
    FakeOperations.latest_fingerprint = None
    FakePersistence.staged = []
    monkeypatch.setattr(execution_module, "RuntimeOperationService", FakeOperations)
    monkeypatch.setattr(execution_module, "EmbeddingPersistence", FakePersistence)

    class FakeCredentialResolver:
        def __init__(self, _session, _crypto):
            pass

        async def resolve_provider(self, _provider_id):
            return SimpleNamespace(api_key="database-test-key", version=7)

    monkeypatch.setattr(execution_module, "ProviderCredentialResolver", FakeCredentialResolver)
    monkeypatch.setattr(execution_module, "ensure_credential_crypto", lambda _settings: object())

    async def resolve(_session, _principal, _model):
        return model, provider, EMBEDDING_PROFILES[model.name]

    monkeypatch.setattr(execution_module, "resolve_embedding_model", resolve)
    calls = {"provider": 0}

    class FakeProvider:
        def __init__(self, **kwargs):
            calls["api_key"] = kwargs["api_key"]

        async def embed(self, *_args, **_kwargs):
            calls["provider"] += 1
            if provider_behavior:
                return await provider_behavior()
            return EmbeddingProviderResult(model=model.name, vectors=[[0.5, -1.25]], input_tokens=10, latency_ms=4)

    monkeypatch.setattr(execution_module, "OpenAIEmbeddingProvider", FakeProvider)
    monkeypatch.setenv(provider.secret_env_var, "legacy-environment-key-must-be-ignored")
    return model, provider, config, settings, calls


@pytest.mark.asyncio
async def test_embedding_receipt_usage_cost_and_single_debit(monkeypatch):
    model, provider, config, settings, calls = harness(monkeypatch)
    service = execution_module.EmbeddingExecutionService(FakeSession(config), settings)
    response = await service.execute(
        fake_request(),
        ApplicationPrincipal("app-1", "key-1"),
        EmbeddingRequest(model=model.name, input="hello", dimensions=2),
        idempotency_key="embed-success",
        request_id="request-1",
    )

    assert response.status_code == 200
    body = response.body.decode()
    assert '"index":0' in body
    operation_receipt = FakeOperations.latest.public_result_json
    assert operation_receipt["usage"]["pipeline"] == {
        "input_tokens": 10,
        "output_tokens": 0,
        "total_tokens": 10,
    }
    assert operation_receipt["cost"]["estimated_cost"] == 0.00002
    assert FakePersistence.staged[0][0][5].usage.complete is True
    assert calls["provider"] == 1
    assert calls["api_key"] == "database-test-key"


@pytest.mark.asyncio
async def test_success_replay_skips_current_catalog_and_provider(monkeypatch):
    model, provider, config, settings, calls = harness(monkeypatch)
    service = execution_module.EmbeddingExecutionService(FakeSession(config), settings)
    principal = ApplicationPrincipal("app-1", "key-1")
    first = await service.execute(
        fake_request(),
        principal,
        EmbeddingRequest(model=model.name, input="hello", dimensions=2),
        idempotency_key="embed-replay",
        request_id="request-1",
    )
    monkeypatch.setattr(
        execution_module, "resolve_embedding_model", lambda *_args: pytest.fail("catalog preflight on replay")
    )
    replay = await service.execute(
        fake_request(),
        principal,
        EmbeddingRequest(model=model.name, input="hello", dimensions=2),
        idempotency_key="embed-replay",
        request_id="request-2",
    )
    assert replay.status_code == first.status_code == 200
    assert replay.body == first.body
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.headers["x-request-id"] == "operation-1"
    assert calls["provider"] == 1


@pytest.mark.asyncio
async def test_provider_failure_replay_keeps_error_and_does_not_call_again(monkeypatch):
    async def fail():
        raise EmbeddingProviderError("private upstream detail", status_code=502)

    model, provider, config, settings, calls = harness(monkeypatch, fail)
    service = execution_module.EmbeddingExecutionService(FakeSession(config), settings)
    principal = ApplicationPrincipal("app-1", "key-1")
    first = await service.execute(
        fake_request(),
        principal,
        EmbeddingRequest(model=model.name, input="hello", dimensions=2),
        idempotency_key="embed-failed",
        request_id="request-1",
    )
    replay = await service.execute(
        fake_request(),
        principal,
        EmbeddingRequest(model=model.name, input="hello", dimensions=2),
        idempotency_key="embed-failed",
        request_id="request-2",
    )
    assert first.status_code == replay.status_code == 502
    assert first.body == replay.body
    assert b"private upstream detail" not in first.body
    assert replay.headers["idempotency-replayed"] == "true"
    assert calls["provider"] == 1


@pytest.mark.asyncio
async def test_zero_retry_after_is_identical_on_initial_provider_failure_and_replay(monkeypatch):
    async def fail():
        raise EmbeddingProviderError("rate limited", status_code=429, retry_after=0)

    model, provider, config, settings, calls = harness(monkeypatch, fail)
    service = execution_module.EmbeddingExecutionService(FakeSession(config), settings)
    principal = ApplicationPrincipal("app-1", "key-1")
    request_payload = EmbeddingRequest(model=model.name, input="hello", dimensions=2)
    first = await service.execute(
        fake_request(),
        principal,
        request_payload,
        idempotency_key="embed-zero-retry-after",
        request_id="request-1",
    )
    replay = await service.execute(
        fake_request(),
        principal,
        request_payload,
        idempotency_key="embed-zero-retry-after",
        request_id="request-2",
    )

    assert first.status_code == replay.status_code == 429
    assert first.headers.get("Retry-After") == replay.headers.get("Retry-After") == "0"
    assert replay.headers["idempotency-replayed"] == "true"
    assert calls["provider"] == 1


@pytest.mark.asyncio
async def test_changed_embedding_body_conflicts_before_catalog_or_provider(monkeypatch):
    model, provider, config, settings, calls = harness(monkeypatch)
    service = execution_module.EmbeddingExecutionService(FakeSession(config), settings)
    principal = ApplicationPrincipal("app-1", "key-1")
    await service.execute(
        fake_request(),
        principal,
        EmbeddingRequest(model=model.name, input="hello", dimensions=2),
        idempotency_key="embed-body-conflict",
        request_id="request-1",
    )
    monkeypatch.setattr(
        execution_module, "resolve_embedding_model", lambda *_args: pytest.fail("catalog read on conflict")
    )
    with pytest.raises(Exception) as error:
        await service.execute(
            fake_request(),
            principal,
            EmbeddingRequest(model=model.name, input="different", dimensions=2),
            idempotency_key="embed-body-conflict",
            request_id="request-2",
        )
    assert error.value.status_code == 409
    assert calls["provider"] == 1


@pytest.mark.asyncio
async def test_result_limit_fails_terminally_and_keeps_measured_receipt(monkeypatch):
    model, provider, config, settings, calls = harness(monkeypatch)
    settings.gateway_embeddings_max_result_bytes = 1
    service = execution_module.EmbeddingExecutionService(FakeSession(config), settings)
    response = await service.execute(
        fake_request(),
        ApplicationPrincipal("app-1", "key-1"),
        EmbeddingRequest(model=model.name, input="hello", dimensions=2),
        idempotency_key="embed-too-large",
        request_id="request-1",
    )
    assert response.status_code == 502
    assert "embedding_result_too_large" in response.body.decode()
    assert FakeOperations.latest.status == "failed"
    assert FakeOperations.latest.public_result_json["usage"]["pipeline"]["input_tokens"] == 10
    assert FakeOperations.latest.public_result_json["cost"]["estimated_cost"] == 0.00002
    assert calls["provider"] == 1


@pytest.mark.asyncio
async def test_commit_failure_marks_indeterminate_and_never_reexecutes(monkeypatch):
    model, provider, config, settings, calls = harness(monkeypatch)
    session = FakeSession(config)
    session.fail_commits = 1
    service = execution_module.EmbeddingExecutionService(session, settings)
    principal = ApplicationPrincipal("app-1", "key-1")
    first = await service.execute(
        fake_request(),
        principal,
        EmbeddingRequest(model=model.name, input="hello", dimensions=2),
        idempotency_key="embed-commit-failure",
        request_id="request-1",
    )
    assert first.status_code == 503
    assert FakeOperations.latest.status == "indeterminate"
    with pytest.raises(Exception) as error:
        await service.execute(
            fake_request(),
            principal,
            EmbeddingRequest(model=model.name, input="hello", dimensions=2),
            idempotency_key="embed-commit-failure",
            request_id="request-2",
        )
    assert error.value.status_code == 409
    assert calls["provider"] == 1
