import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.backend_ai.routers import openai_responses
from src.backend_ai.routers.openai_errors import OpenAIAPIError, openai_error_handler
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.runtime_operation_service import RuntimeStorageError
from src.shared.dtos.gateway import OutputEvidence
from src.shared.dtos.runtime_api import (
    RuntimeCost,
    RuntimeRequestResponse,
    RuntimeUsage,
    RuntimeUsageDetails,
    RuntimeUsageScope,
)


@pytest.fixture(autouse=True)
def mocked_operation_lookup_for_route_unit_tests(monkeypatch):
    from unittest.mock import AsyncMock

    from src.backend_ai.services.runtime_operation_service import RuntimeOperationService
    monkeypatch.setattr(RuntimeOperationService, "lookup_normalized", AsyncMock(return_value=None))


def make_request(body: dict, headers: dict[str, str] | None = None) -> Request:
    headers = {"Idempotency-Key": "route-test", **(headers or {})}
    raw_body = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/responses",
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
        "query_string": b"",
        "state": {},
    }
    return Request(scope, receive)


def receipt() -> RuntimeRequestResponse:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    return RuntimeRequestResponse(
        request_id="operation-1",
        application_id="app-1",
        status="completed",
        execution_mode="live",
        output="Hello from runtime",
        models_used=["model-id"],
        providers_used=["openai"],
        usage=RuntimeUsage(
            execution=RuntimeUsageScope(input_tokens=6, output_tokens=4, total_tokens=10),
            pipeline=RuntimeUsageScope(input_tokens=8, output_tokens=5, total_tokens=13),
            complete=True,
        ),
        usage_details=RuntimeUsageDetails(
            cached_tokens=1, cache_write_tokens=2, reasoning_tokens=1, complete=True
        ),
        output_evidence=OutputEvidence(
            finish_reason="stop", cached_tokens=1, cache_write_tokens=2, reasoning_tokens=1
        ),
        cost=RuntimeCost(estimated_cost=0.0002),
        latency_ms=2,
        config_version=1,
        started_at=now,
        finished_at=now,
    )


@pytest.mark.asyncio
async def test_model_name_lookup_uses_operation_snapshot_and_skips_invalid_rows(monkeypatch):
    principal = ApplicationPrincipal("app-1", "key-1")

    class FakeOperations:
        def __init__(self, _session):
            pass

        async def read(self, actual_principal, request_id):
            assert actual_principal is principal
            assert request_id == "operation-1"
            return SimpleNamespace(config_snapshot_json={"models": [
                {"id": "internal-id", "name": "public-model"},
                {"id": "bad-name"},
                "bad-row",
            ]})

    monkeypatch.setattr(openai_responses, "RuntimeOperationService", FakeOperations)
    assert await openai_responses._model_names_from_snapshot(object(), principal, "operation-1") == {
        "internal-id": "public-model",
    }


@pytest.mark.asyncio
async def test_snapshot_read_failure_preserves_completed_operation_replay_headers(monkeypatch):
    async def fake_execute(*_args, **_kwargs):
        return JSONResponse(receipt().model_dump(mode="json"), headers={
            "x-request-id": "operation-1",
            "idempotency-key": "same-key",
            "idempotency-replayed": "true",
        })

    async def fail_model_names(*_args):
        raise RuntimeStorageError("temporary read failure")

    monkeypatch.setattr(openai_responses, "execute_runtime_request", fake_execute)
    monkeypatch.setattr(openai_responses, "_model_names_from_snapshot", fail_model_names)
    request = make_request(
        {"model": "auto", "input": "hello", "store": False}, {"Idempotency-Key": "same-key"}
    )

    with pytest.raises(OpenAIAPIError) as error:
        await openai_responses.create_response(request, ApplicationPrincipal("app-1", "key-1"), object())
    rendered = await openai_error_handler(request, error.value)

    assert rendered.status_code == 503
    assert rendered.headers["x-request-id"] == "operation-1"
    assert rendered.headers["x-gateway-request-id"] == "operation-1"
    assert rendered.headers["idempotency-key"] == "same-key"
    assert rendered.headers["idempotency-replayed"] == "true"


@pytest.mark.asyncio
async def test_route_projects_runtime_output_and_preserves_history_and_replay_headers(monkeypatch):
    calls = []

    async def fake_execute(request, principal, session, payload, **kwargs):
        calls.append((payload, kwargs))
        result = SimpleNamespace(aggregation=None)
        assert kwargs["output_policy"](result).error is None
        return JSONResponse(
            receipt().model_dump(mode="json"),
            headers={
                "x-request-id": "operation-1",
                "idempotency-key": "same-key",
                "idempotency-replayed": "true",
            },
        )

    monkeypatch.setattr(openai_responses, "execute_runtime_request", fake_execute)
    async def fake_model_names(*_args):
        return {"model-id": "gpt-4.1-nano"}

    monkeypatch.setattr(openai_responses, "_model_names_from_snapshot", fake_model_names)
    body = {
        "model": "auto:fast",
        "input": [
            {"role": "user", "content": "first"},
            {"id": "msg_old", "type": "message", "role": "assistant", "status": "completed",
             "phase": "final_answer",
             "content": [{"type": "output_text", "text": "prior answer", "annotations": [], "logprobs": []}]},
            {"role": "user", "content": "next"},
        ],
        "instructions": "Be concise",
        "metadata": {"source": "test"},
        "store": False,
    }
    request = make_request(body, {"Idempotency-Key": "same-key"})
    response = await openai_responses.create_response(
        request, ApplicationPrincipal("app-1", "key-1"), object()
    )

    content = json.loads(response.body)
    assert content["object"] == "response"
    assert content["model"] == "gpt-4.1-nano"
    assert content["output"][0]["content"][0]["text"] == "Hello from runtime"
    assert content["usage"]["total_tokens"] == 10
    runtime_payload, kwargs = calls[0]
    assert [message.role for message in runtime_payload.messages] == ["developer", "user", "assistant", "user"]
    assert runtime_payload.constraints.quality_priority == "latency_first"
    assert kwargs["operation_fingerprint"].normalization_version == 2
    assert response.headers["idempotency-replayed"] == "true"
    assert response.headers["x-request-id"] == "operation-1"


@pytest.mark.asyncio
async def test_route_uses_typed_buffered_sse(monkeypatch):
    async def fake_execute(request, principal, session, payload, **kwargs):
        return JSONResponse(receipt().model_dump(mode="json"), headers={"x-request-id": "operation-1"})

    monkeypatch.setattr(openai_responses, "execute_runtime_request", fake_execute)
    async def fake_model_names(*_args):
        return {"model-id": "gpt-4.1-nano"}

    monkeypatch.setattr(openai_responses, "_model_names_from_snapshot", fake_model_names)
    request = make_request({"model": "auto", "input": "hello", "store": False, "stream": True})
    response = await openai_responses.create_response(
        request, ApplicationPrincipal("app-1", "key-1"), object()
    )
    assert isinstance(response, StreamingResponse)
    assert response.headers["x-gateway-streaming-mode"] == "buffered"
    frames = []
    async for frame in response.body_iterator:
        frames.append(frame)
    assert "event: response.completed" in "".join(frames)
    assert "[DONE]" not in "".join(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"model": "auto", "input": "x", "store": False, "reasoning": {"effort": "high"}},
        {"model": "auto", "input": "x", "store": False, "max_tool_calls": 10},
        {"model": "auto", "input": "x", "store": False, "text": {"format": {"type": "json_schema", "name": "x", "schema": {"patternProperties": {}}}}},
        {"model": "auto", "input": "x", "store": False, "background": True},
    ],
)
async def test_unsupported_parameters_fail_before_runtime(monkeypatch, body):
    async def unexpected(*args, **kwargs):
        raise AssertionError("unsupported input must not reach runtime")

    monkeypatch.setattr(openai_responses, "execute_runtime_request", unexpected)
    request = make_request(body)
    with pytest.raises(OpenAIAPIError) as error:
        await openai_responses.create_response(request, ApplicationPrincipal("app", "key"), object())
    assert error.value.status_code == 400
