import json

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from src.backend_ai.routers import openai_embeddings
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.shared.config import Settings


def make_request(body: dict, headers: dict[str, str] | None = None) -> Request:
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
        "path": "/v1/embeddings",
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
        "query_string": b"",
        "state": {},
    }
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_route_rejects_unknown_parameter_with_exact_param(monkeypatch):
    monkeypatch.setattr(openai_embeddings, "get_settings", lambda: Settings(
        _env_file=None, database_url="sqlite+aiosqlite://"
    ))
    request = make_request({"model": "text-embedding-3-small", "input": "hello", "stream": True})
    with pytest.raises(OpenAIAPIError) as error:
        await openai_embeddings.create_embedding(request, ApplicationPrincipal("app", "key"), object())
    assert error.value.status_code == 400
    assert error.value.param == "stream"
    assert request.state.request_id


@pytest.mark.asyncio
async def test_route_serializes_controlled_embedding_result(monkeypatch):
    captured = {}

    class FakeService:
        def __init__(self, _session):
            pass

        async def execute(self, request, principal, payload, *, idempotency_key, request_id):
            captured.update(payload=payload, key=idempotency_key, request_id=request_id)
            return JSONResponse(
                {"object": "list", "model": payload.model, "usage": {"prompt_tokens": 3, "total_tokens": 3},
                 "data": [{"object": "embedding", "index": 0, "embedding": [0.25, -2.5]}]},
                headers={"x-request-id": "operation-id", "x-gateway-request-id": "operation-id"},
            )

    monkeypatch.setattr(openai_embeddings, "EmbeddingExecutionService", FakeService)
    monkeypatch.setattr(openai_embeddings, "get_settings", lambda: Settings(
        _env_file=None, database_url="sqlite+aiosqlite://"
    ))
    request = make_request(
        {"model": "text-embedding-3-small", "input": "hello", "encoding_format": "base64"},
        {"Idempotency-Key": "route-success"},
    )
    response = await openai_embeddings.create_embedding(
        request, ApplicationPrincipal("app", "key"), object()
    )
    assert json.loads(response.body)["data"][0]["embedding"] == [0.25, -2.5]
    assert response.headers["x-request-id"] == "operation-id"
    assert captured["key"] == "route-success"
    assert captured["payload"].encoding_format == "base64"
