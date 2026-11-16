from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class MemoryOperationRepository:
    def __init__(self):
        self.records = {}

    async def by_idempotency_key(self, application_id, key_hash):
        return self.records.get((application_id, key_hash))

    async def reconcile_expired(self, application_id, now):
        count = 0
        for (record_application_id, _), operation in self.records.items():
            if (
                record_application_id == application_id
                and operation.status == "running"
                and operation.expires_at <= now
            ):
                operation.status = "indeterminate"
                count += 1
        return count

    async def count_since(self, application_id, since):
        return sum(
            application_id == operation.application_id and operation.started_at >= since
            for operation in self.records.values()
        )

    async def count_running(self, application_id, now):
        return sum(
            application_id == operation.application_id
            and operation.status == "running"
            and operation.expires_at > now
            for operation in self.records.values()
        )

    async def add(self, operation):
        self.records[(operation.application_id, operation.idempotency_key_hash)] = operation


@pytest.fixture
def operation_service():
    from src.backend_ai.services.runtime_operation_service import RuntimeOperationService

    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    service = RuntimeOperationService(session)
    service.repository = MemoryOperationRepository()
    service._begin_serialized_transaction = AsyncMock()
    return service


def _principal(application_id="app-a", key_id="key-a"):
    from src.backend_ai.services.application_auth_service import ApplicationPrincipal

    return ApplicationPrincipal(application_id=application_id, key_id=key_id)


def _snapshot(version=7):
    return {"config_version": version, "model": "model-at-first-request"}



def test_v2_fingerprint_is_canonical_and_has_an_independent_digest():
    from src.backend_ai.services.openai_operation_fingerprint import (
        OperationFingerprint,
        build_openai_fingerprint,
    )

    payload = {
        "input": ["x", "y"],
        "model": "m",
        "encoding_format": "float",
    }
    result = build_openai_fingerprint("openai_embeddings", payload)

    assert isinstance(result, OperationFingerprint)
    assert result.normalization_version == 2
    assert result.payload_hash == "31ebcae2b8a3421e13b5f02abe750fea4cf23d5cc9efce3c8a8212ad5fd35df8"


def test_v2_fingerprint_preserves_surface_field_and_input_order():
    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint

    payload = {
        "model": "m",
        "input": ["alpha", "beta"],
        "dimensions": 256,
        "encoding_format": "float",
        "user": "u-1",
        "stream": False,
        "instructions": "follow the requested format",
        "text": {"format": {"type": "text"}},
        "metadata": {"job": "42"},
    }
    expected = build_openai_fingerprint("openai_responses", payload)

    assert expected != build_openai_fingerprint("openai_embeddings", payload)
    for key, changed_value in (
        ("model", "different-model"),
        ("input", ["beta", "alpha"]),
        ("dimensions", 512),
        ("encoding_format", "base64"),
        ("user", "u-2"),
        ("stream", True),
        ("instructions", "different"),
        ("text", {"format": {"type": "json_object"}}),
        ("metadata", {"job": "43"}),
    ):
        modified = {**payload, key: changed_value}
        assert expected.payload_hash != build_openai_fingerprint("openai_responses", modified).payload_hash


def test_v2_fingerprint_rejects_non_finite_json_values():
    import math

    import pytest

    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint

    with pytest.raises(ValueError):
        build_openai_fingerprint("openai_embeddings", {"input": math.nan})


def test_v1_historical_fingerprint_is_unchanged():
    from src.backend_ai.services.runtime_operation_service import RuntimeOperationService
    from src.shared.dtos.runtime_api import RuntimeRequestInput

    assert RuntimeOperationService.fingerprint(RuntimeRequestInput(prompt="hello")) == (
        "78369253c7cba1af311f840e4a930afb0298a31f6b574896927d98e241bca1e5"
    )


@pytest.mark.asyncio
async def test_normalized_operations_conflict_across_openai_surfaces(operation_service):
    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint

    now = datetime(2026, 10, 7, tzinfo=UTC)
    responses = build_openai_fingerprint("openai_responses", {"model": "m", "input": "hello"})
    embeddings = build_openai_fingerprint("openai_embeddings", {"model": "m", "input": "hello"})
    first = await operation_service.claim_normalized(
        _principal(), "shared-key", responses, config_version=7, config_snapshot=_snapshot(),
        execution_mode="offline", now=now,
    )
    collision = await operation_service.claim_normalized(
        _principal(), "shared-key", embeddings, config_version=8, config_snapshot=_snapshot(8),
        execution_mode="offline", now=now,
    )

    assert first.kind == "owner"
    assert collision.kind == "conflict"
    assert collision.operation.request_id == first.operation.request_id
    assert collision.operation.normalization_version == 2


@pytest.mark.asyncio
async def test_two_keys_for_same_application_share_normalized_replay(operation_service):
    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint

    now = datetime(2026, 10, 7, tzinfo=UTC)
    fingerprint = build_openai_fingerprint("openai_responses", {"model": "m", "input": "hello"})
    owner = await operation_service.claim_normalized(
        _principal(), "same-key", fingerprint, config_version=7, config_snapshot=_snapshot(),
        execution_mode="offline", now=now,
    )
    replay = await operation_service.lookup_normalized(
        _principal(key_id="key-b"), "same-key", fingerprint, now=now,
    )

    assert owner.kind == "owner"
    assert replay.kind == "running"
    assert replay.operation.request_id == owner.operation.request_id


@pytest.mark.asyncio
async def test_other_application_can_use_same_normalized_idempotency_key(operation_service):
    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint

    now = datetime(2026, 10, 7, tzinfo=UTC)
    fingerprint = build_openai_fingerprint("openai_responses", {"model": "m", "input": "hello"})
    first = await operation_service.claim_normalized(
        _principal(), "same-key", fingerprint, config_version=7, config_snapshot=_snapshot(),
        execution_mode="offline", now=now,
    )
    other_app = await operation_service.claim_normalized(
        _principal(application_id="app-b", key_id="key-c"), "same-key", fingerprint,
        config_version=7, config_snapshot=_snapshot(), execution_mode="offline", now=now,
    )

    assert first.kind == other_app.kind == "owner"
    assert first.operation.request_id != other_app.operation.request_id


@pytest.mark.asyncio
async def test_normalized_terminal_lookup_replays_saved_snapshot(operation_service):
    from src.backend_ai.services.openai_operation_fingerprint import build_openai_fingerprint

    now = datetime(2026, 10, 7, tzinfo=UTC)
    fingerprint = build_openai_fingerprint("openai_embeddings", {"model": "m", "input": ["hello"]})
    owner = await operation_service.claim_normalized(
        _principal(), "catalog-key", fingerprint, config_version=7, config_snapshot=_snapshot(),
        execution_mode="offline", now=now,
    )
    owner.operation.status = "completed"
    owner.operation.public_result_json = {"vector": [1.0]}
    owner.operation.http_status = 200
    owner.operation.http_body_json = {"data": [1.0]}
    owner.operation.finished_at = now + timedelta(seconds=1)
    replay = await operation_service.lookup_normalized(
        _principal(), "catalog-key", fingerprint, now=now + timedelta(days=30),
    )

    assert replay.kind == "replay"
    assert replay.operation.config_version == 7
    assert replay.operation.config_snapshot_json == _snapshot()
    assert replay.operation.http_body_json == {"data": [1.0]}


@pytest.mark.asyncio
async def test_native_v1_wrappers_keep_the_historical_hash_and_claim_version(operation_service):
    from src.backend_ai.services.runtime_operation_service import RuntimeOperationService
    from src.shared.dtos.runtime_api import RuntimeRequestInput

    now = datetime(2026, 10, 7, tzinfo=UTC)
    payload = RuntimeRequestInput(prompt="hello")
    expected_v1_hash = "78369253c7cba1af311f840e4a930afb0298a31f6b574896927d98e241bca1e5"
    owner = await operation_service.claim(
        _principal(), "native-key", payload, config_version=7, config_snapshot=_snapshot(), now=now,
    )
    replay = await operation_service.lookup(_principal(), "native-key", payload, now=now)

    assert owner.kind == "owner"
    assert owner.operation.normalization_version == RuntimeOperationService.NORMALIZATION_VERSION == 1
    assert owner.operation.payload_hash == expected_v1_hash
    assert replay.kind == "running"
