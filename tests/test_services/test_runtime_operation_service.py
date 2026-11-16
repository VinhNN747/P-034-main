from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.shared.models  # noqa: F401
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.runtime_operation_service import (
    InvalidIdempotencyKeyError,
    RuntimeOperationService,
)
from src.shared.database.base import Base
from src.shared.dtos.runtime_api import RuntimeRequestInput
from src.shared.models.core import Application
from src.shared.models.integration import ApplicationApiKeyRecord

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def operation_store(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "operations.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Application(
                id="app-operations",
                slug="operations-app",
                name="Operations App",
                owner_user_id="owner-1",
                environment="development",
                status="active",
            )
        )
        session.add(
            ApplicationApiKeyRecord(
                id="key-operations",
                application_id="app-operations",
                name="runtime",
                key_prefix="ak_123456789",
                key_hash="a" * 64,
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


def principal() -> ApplicationPrincipal:
    return ApplicationPrincipal(application_id="app-operations", key_id="key-operations")


def snapshot() -> dict:
    return {"routing_objective": "balanced", "pool": [{"id": "model-1", "price": 0.1}]}


async def claim(factory, *, key="operation-1", payload=None, **kwargs):
    now = kwargs.pop("now", NOW)
    async with factory() as session:
        return await RuntimeOperationService(session, **kwargs).claim(
            principal(),
            key,
            payload or RuntimeRequestInput(prompt="hello"),
            config_version=7,
            config_snapshot=snapshot(),
            now=now,
        )


async def finalize(factory, request_id, **kwargs):
    async with factory() as session:
        return await RuntimeOperationService(session).finalize(request_id, **kwargs)


@pytest.mark.asyncio
async def test_same_key_claims_once_and_payload_changes_conflict(operation_store):
    first = await claim(operation_store)
    duplicate = await claim(operation_store)
    conflict = await claim(operation_store, payload=RuntimeRequestInput(prompt="different"))

    assert first.kind == "owner"
    assert duplicate.kind == "running"
    assert duplicate.operation.request_id == first.operation.request_id
    assert conflict.kind == "conflict"
    assert conflict.operation.request_id == first.operation.request_id


@pytest.mark.asyncio
async def test_completed_receipt_replays_unchanged_and_cas_prevents_late_finalize(operation_store):
    first = await claim(operation_store)
    receipt = {"request_id": first.operation.request_id, "status": "completed", "output": "answer"}
    assert await finalize(
        operation_store,
        first.operation.request_id,
        status="completed",
        public_result=receipt,
        http_status=200,
        http_body=receipt,
        now=NOW + timedelta(seconds=2),
    )
    replay = await claim(operation_store)
    assert replay.kind == "replay"
    assert replay.operation.http_status == 200
    assert replay.operation.http_body_json == receipt
    assert replay.operation.public_result_json == receipt
    assert not await finalize(
        operation_store,
        first.operation.request_id,
        status="completed",
        public_result={"output": "late"},
        http_status=200,
        http_body={"output": "late"},
        now=NOW + timedelta(seconds=3),
    )


@pytest.mark.asyncio
async def test_fingerprint_normalizes_defaults_nulls_and_unordered_allow_lists(operation_store):
    omitted = RuntimeRequestInput(prompt="hello")
    explicit_defaults = RuntimeRequestInput.model_validate(
        {"prompt": "hello", "context": {}, "metadata": {}, "constraints": {"allow_partial_response": False}}
    )
    unordered = RuntimeRequestInput.model_validate(
        {"prompt": "hello", "constraints": {"allowed_models": ["b", "a", "b"], "quality_priority": None}}
    )
    ordered = RuntimeRequestInput.model_validate(
        {"prompt": "hello", "constraints": {"allowed_models": ["a", "b"]}}
    )
    async with operation_store() as session:
        service = RuntimeOperationService(session)
        assert service.fingerprint(omitted) == service.fingerprint(explicit_defaults)
        assert service.fingerprint(unordered) == service.fingerprint(ordered)
        assert service.fingerprint(omitted) != service.fingerprint(
            RuntimeRequestInput.model_validate({"prompt": "hello", "constraints": {"quality_priority": "balanced"}})
        )


@pytest.mark.asyncio
async def test_limits_apply_only_to_new_operations_and_expired_work_is_indeterminate(operation_store):
    limited = await claim(operation_store, max_concurrent_requests=1, requests_per_minute=1)
    duplicate = await claim(operation_store, key="operation-1", max_concurrent_requests=1, requests_per_minute=1)
    blocked_by_concurrency = await claim(operation_store, key="operation-2", max_concurrent_requests=1)
    assert limited.kind == "owner"
    assert duplicate.kind == "running"
    assert blocked_by_concurrency.kind == "concurrency_limited"

    unknown = await claim(
        operation_store,
        key="operation-1",
        now=NOW + timedelta(seconds=151),
        max_concurrent_requests=1,
    )
    assert unknown.kind == "unknown"
    assert unknown.operation.status == "indeterminate"
    assert not await finalize(
        operation_store,
        limited.operation.request_id,
        status="completed",
        public_result={"status": "completed"},
        http_status=200,
        http_body={"status": "completed"},
    )


@pytest.mark.asyncio
async def test_rate_limit_rejects_new_claim_but_keeps_same_key_replay(operation_store):
    first = await claim(operation_store, requests_per_minute=1)
    await finalize(
        operation_store,
        first.operation.request_id,
        status="failed",
        public_result={"request_id": first.operation.request_id, "status": "failed"},
        http_status=502,
        http_body={"error": {"code": "PROVIDER_EXECUTION_FAILED"}},
        now=NOW + timedelta(seconds=1),
    )
    replay = await claim(operation_store, requests_per_minute=1)
    rate_limited = await claim(operation_store, key="operation-2", requests_per_minute=1)
    assert replay.kind == "terminal_replay"
    assert replay.operation.http_status == 502
    assert rate_limited.kind == "rate_limited"


@pytest.mark.asyncio
async def test_read_is_application_scoped_and_reconciles_stale_running_operation(operation_store):
    started = await claim(operation_store)
    async with operation_store() as session:
        service = RuntimeOperationService(session)
        own = await service.read(principal(), started.operation.request_id, now=NOW)
        other = await service.read(
            ApplicationPrincipal(application_id="some-other-app", key_id="key-operations"),
            started.operation.request_id,
            now=NOW,
        )
        stale = await service.read(
            principal(), started.operation.request_id, now=NOW + timedelta(seconds=151)
        )
    assert own is not None
    assert other is None
    assert stale is not None and stale.status == "indeterminate"


@pytest.mark.asyncio
async def test_concurrent_duplicate_claims_have_only_one_owner(operation_store):
    import asyncio

    results = await asyncio.gather(
        claim(operation_store, key="parallel"),
        claim(operation_store, key="parallel"),
    )
    assert [result.kind for result in results].count("owner") == 1
    assert len({result.operation.request_id for result in results}) == 1


@pytest.mark.asyncio
async def test_normalized_operations_conflict_across_openai_surfaces(operation_store):
    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint

    responses = build_openai_fingerprint("openai_responses", {"model": "m", "input": "hello"})
    embeddings = build_openai_fingerprint("openai_embeddings", {"model": "m", "input": "hello"})
    async with operation_store() as session:
        service = RuntimeOperationService(session)
        first = await service.claim_normalized(
            principal(), "shared-key", responses, config_version=7, config_snapshot=snapshot(), execution_mode="offline",
            now=NOW,
        )
        collision = await service.claim_normalized(
            principal(), "shared-key", embeddings, config_version=8, config_snapshot={"changed": True},
            execution_mode="offline", now=NOW,
        )

    assert first.kind == "owner"
    assert collision.kind == "conflict"
    assert collision.operation.request_id == first.operation.request_id
    assert collision.operation.normalization_version == 2


@pytest.mark.asyncio
async def test_two_keys_for_same_application_share_normalized_replay(operation_store):
    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint

    fingerprint = build_openai_fingerprint("openai_responses", {"model": "m", "input": "hello"})
    async with operation_store() as session:
        owner = await RuntimeOperationService(session).claim_normalized(
            principal(), "same-key", fingerprint, config_version=7, config_snapshot=snapshot(),
            execution_mode="offline", now=NOW,
        )
        second_key = ApplicationPrincipal(application_id="app-operations", key_id="another-key")
        replay = await RuntimeOperationService(session).lookup_normalized(
            second_key, "same-key", fingerprint, now=NOW,
        )

    assert owner.kind == "owner"
    assert replay.kind == "running"
    assert replay.operation.request_id == owner.operation.request_id


@pytest.mark.asyncio
async def test_normalized_terminal_lookup_replays_saved_snapshot(operation_store):
    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint

    fingerprint = build_openai_fingerprint("openai_embeddings", {"model": "m", "input": ["hello"]})
    async with operation_store() as session:
        service = RuntimeOperationService(session)
        owner = await service.claim_normalized(
            principal(), "catalog-key", fingerprint, config_version=7, config_snapshot=snapshot(),
            execution_mode="offline", now=NOW,
        )
        await service.finalize(
            owner.operation.request_id, status="completed", public_result={"vector": [1.0]},
            http_status=200, http_body={"data": [1.0]}, now=NOW + timedelta(seconds=1),
        )
        # No model/catalog is consulted by lookup; it returns the immutable
        # operation snapshot even when the caller's current catalog is absent.
        replay = await service.lookup_normalized(
            principal(), "catalog-key", fingerprint, now=NOW + timedelta(days=30),
        )

    assert replay.kind == "replay"
    assert replay.operation.config_version == 7
    assert replay.operation.config_snapshot_json == snapshot()
    assert replay.operation.http_body_json == {"data": [1.0]}


def test_idempotency_key_format_is_bounded_visible_ascii():
    assert RuntimeOperationService.validate_idempotency_key("job-123") == "job-123"
    for value in ("", "has space", "line\nbreak", "é", "x" * 129):
        with pytest.raises(InvalidIdempotencyKeyError):
            RuntimeOperationService.validate_idempotency_key(value)
