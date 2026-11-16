from __future__ import annotations

import asyncio
import base64
import socket
import struct
from datetime import UTC, datetime

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI, OpenAI

from src.backend_ai.dtos.openai_compat import ModelCard, ModelList
from src.backend_ai.dtos.openai_embeddings import EmbeddingResponse
from src.backend_ai.routers import openai_compat, openai_embeddings, openai_errors, openai_responses
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.openai_request_context import install_openai_request_context
from src.shared.dtos.gateway import OutputEvidence
from src.shared.dtos.runtime_api import (
    RuntimeCost,
    RuntimeRequestResponse,
    RuntimeUsage,
    RuntimeUsageDetails,
    RuntimeUsageScope,
)


@pytest_asyncio.fixture
async def tcp_openai_gateway(monkeypatch):
    from unittest.mock import AsyncMock

    from src.backend_ai.services.runtime_operation_service import RuntimeOperationService
    monkeypatch.setattr(RuntimeOperationService, "lookup_normalized", AsyncMock(return_value=None))
    from src.shared.config import Settings
    settings = Settings(_env_file=None, gateway_openai_idempotency_policy="best_effort")
    monkeypatch.setattr(openai_responses, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_embeddings, "get_settings", lambda: settings)
    captured = {"responses": [], "embeddings": []}
    application = FastAPI()
    install_openai_request_context(application)
    application.include_router(openai_compat.router)
    application.include_router(openai_embeddings.router)
    application.include_router(openai_responses.router)
    application.add_exception_handler(openai_errors.OpenAIAPIError, openai_errors.openai_error_handler)

    async def principal_override():
        return ApplicationPrincipal("app-tcp", "key-tcp")

    async def db_override():
        yield object()

    from src.backend_ai.dependencies.openai_auth import get_openai_principal
    from src.shared.database.session import get_db

    application.dependency_overrides[get_openai_principal] = principal_override
    application.dependency_overrides[get_db] = db_override

    class Models:
        def __init__(self, session):
            pass

        async def list_visible(self, principal):
            return ModelList(
                data=[
                    ModelCard(id="auto:fast", owned_by="gateway-router"),
                    ModelCard(id="provider/model", owned_by="gateway-pinned"),
                    ModelCard(id="模型", owned_by="gateway-pinned"),
                    ModelCard(id="literal%value", owned_by="gateway-pinned"),
                ]
            )

        async def retrieve_visible(self, principal, model):
            if model not in {"auto:fast", "provider/model", "模型", "literal%value"}:
                raise openai_errors.OpenAIAPIError(404, "not found", code="model_not_found", param="model")
            return ModelCard(id=model, owned_by="gateway-router")

    monkeypatch.setattr(openai_compat, "OpenAIModelService", Models)

    async def fake_runtime(request, principal, session, payload, **kwargs):
        captured["responses"].append({"payload": payload, "kwargs": kwargs})
        now = datetime(2026, 10, 7, tzinfo=UTC)
        answer = "TCP controlled answer"
        receipt = RuntimeRequestResponse(
            request_id="tcp-operation-1",
            application_id=principal.application_id,
            status="completed",
            execution_mode="live",
            output=answer,
            models_used=["model-id"],
            providers_used=["openai"],
            usage=RuntimeUsage(
                execution=RuntimeUsageScope(input_tokens=4, output_tokens=3, total_tokens=7),
                pipeline=RuntimeUsageScope(input_tokens=7, output_tokens=4, total_tokens=11),
                complete=True,
            ),
            usage_details=RuntimeUsageDetails(
                cached_tokens=1, cache_write_tokens=2, reasoning_tokens=1, complete=True
            ),
            output_evidence=OutputEvidence(
                finish_reason="stop", cached_tokens=1, cache_write_tokens=2, reasoning_tokens=1
            ),
            cost=RuntimeCost(estimated_cost=0.0001),
            config_version=1,
            started_at=now,
            finished_at=now,
            latency_ms=2,
        )
        return JSONResponse(
            receipt.model_dump(mode="json"),
            headers={
                "x-request-id": receipt.request_id,
                "idempotency-key": kwargs.get("idempotency_key", ""),
                "idempotency-replayed": "false",
            },
        )

    monkeypatch.setattr(openai_responses, "execute_runtime_request", fake_runtime)

    async def fake_model_names(_session, _principal, _request_id):
        return {"model-id": "gpt-4.1-nano"}

    monkeypatch.setattr(openai_responses, "_model_names_from_snapshot", fake_model_names)

    class Embeddings:
        def __init__(self, session):
            pass

        async def execute(self, request, principal, payload, *, idempotency_key, request_id):
            captured["embeddings"].append(payload.model_dump(mode="json"))
            vectors = [[0.25, -1.5], [2.0, 3.25]] if isinstance(payload.input, list) and payload.input and isinstance(payload.input[0], str) else [[0.25, -1.5]]
            if payload.encoding_format == "base64":
                vectors = [base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode() for vector in vectors]
            response = EmbeddingResponse(
                model=payload.model,
                usage={"prompt_tokens": 5, "total_tokens": 5},
                data=[{"index": index, "embedding": vector} for index, vector in enumerate(vectors)],
            )
            return JSONResponse(
                response.model_dump(mode="json"),
                headers={"x-request-id": "embedding-tcp-id", "idempotency-key": idempotency_key},
            )

    monkeypatch.setattr(openai_embeddings, "EmbeddingExecutionService", Embeddings)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("sandbox policy disallows local TCP sockets")
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    host, port = sock.getsockname()
    server = uvicorn.Server(uvicorn.Config(application, log_level="critical", access_log=False, lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started:
            break
        if task.done():
            await task
        await asyncio.sleep(0.01)
    assert server.started
    try:
        yield f"http://{host}:{port}/v1", captured
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


@pytest.mark.asyncio
async def test_openai_sdk_sync_and_async_models_responses_and_embeddings_over_tcp(tcp_openai_gateway):
    base_url, captured = tcp_openai_gateway

    def sync_calls():
        client = OpenAI(base_url=base_url, api_key="test-key", max_retries=0)
        listed = client.models.list()
        retrieved = client.models.retrieve("auto:fast")
        path_cards = [client.models.retrieve(model_id) for model_id in ("provider/model", "模型", "literal%value")]
        try:
            client.models.retrieve("not-visible")
        except Exception as error:
            unknown_status = error.status_code
        else:
            unknown_status = None
        response = client.responses.create(model="auto", input="hello", store=False)
        response_history = client.responses.create(
            model="auto",
            input=[response.output[0].model_dump(mode="json"), {"role": "user", "content": "follow up"}],
            store=False,
        )
        embedding = client.embeddings.create(model="text-embedding-3-small", input=["one", "two"])
        explicit_base64 = client.embeddings.create(
            model="text-embedding-3-small", input="one", encoding_format="base64"
        )
        client.close()
        return listed, retrieved, path_cards, unknown_status, response, response_history, embedding, explicit_base64

    listed, retrieved, path_cards, unknown_status, response, response_history, embedding, explicit_base64 = await asyncio.to_thread(sync_calls)
    assert listed.data[0].id == retrieved.id == "auto:fast"
    assert [card.id for card in path_cards] == ["provider/model", "模型", "literal%value"]
    assert unknown_status == 404
    assert response.output_text == "TCP controlled answer"
    assert response.model == response_history.model == "gpt-4.1-nano"
    assert response_history.output_text == "TCP controlled answer"
    assert response._request_id == "tcp-operation-1"
    assert response.usage.total_tokens == 7
    assert captured["responses"][1]["payload"].messages[0].role == "assistant"
    assert isinstance(embedding.data[0].embedding, list)
    assert embedding.data[0].embedding == [0.25, -1.5]
    assert isinstance(explicit_base64.data[0].embedding, str)
    assert captured["embeddings"][0]["encoding_format"] == "base64"
    decoded = struct.unpack("<2f", base64.b64decode(explicit_base64.data[0].embedding))
    assert decoded == (0.25, -1.5)

    async with AsyncOpenAI(base_url=base_url, api_key="test-key", max_retries=0) as client:
        listed_async = await client.models.list()
        retrieved_async = await client.models.retrieve("auto:fast")
        path_card_async = await client.models.retrieve("provider/model")
        response_async = await client.responses.create(model="auto", input="hello", store=False)
        response_history_async = await client.responses.create(
            model="auto",
            input=[response_async.output[0].model_dump(mode="json"), {"role": "user", "content": "follow up"}],
            store=False,
        )
        embedding_async = await client.embeddings.create(
            model="text-embedding-3-small", input="one", encoding_format="float"
        )
    assert listed_async.data[0].id == retrieved_async.id == "auto:fast"
    assert path_card_async.id == "provider/model"
    assert response_async.output_text == "TCP controlled answer"
    assert response_async.model == response_history_async.model == "gpt-4.1-nano"
    assert response_history_async.output_text == "TCP controlled answer"
    assert response_async._request_id == "tcp-operation-1"
    assert embedding_async.data[0].embedding == [0.25, -1.5]


@pytest.mark.asyncio
async def test_openai_sdk_sync_and_async_responses_stream_helpers_over_tcp(tcp_openai_gateway):
    base_url, _ = tcp_openai_gateway

    def sync_stream():
        client = OpenAI(base_url=base_url, api_key="test-key", max_retries=0)
        with client.responses.stream(model="auto", input="hello", store=False) as stream:
            events = list(stream)
            response = stream.get_final_response()
        client.close()
        return events, response

    sync_events, sync_final = await asyncio.to_thread(sync_stream)
    assert sync_events[-1].type == "response.completed"
    assert sync_final.output_text == "TCP controlled answer"

    async with AsyncOpenAI(base_url=base_url, api_key="test-key", max_retries=0) as client:
        async with client.responses.stream(model="auto", input="hello", store=False) as stream:
            async_events = [event async for event in stream]
            async_final = await stream.get_final_response()
    assert async_events[-1].type == "response.completed"
    assert async_final.output_text == "TCP controlled answer"
