from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request

from src.backend_ai.latency.context import current_latency_context
from src.backend_ai.latency.contracts import HistorySnapshot, LatencyConfig, ModelKey
from src.shared.config import Settings


def _request(app: FastAPI) -> Request:
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/runtime/requests",
        "raw_path": b"/api/v1/runtime/requests",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 1),
        "app": app,
    })


def test_shared_native_and_openai_runtime_factory_uses_lifespan_runtime(monkeypatch):
    from src.backend_ai.routers import runtime

    expected_settings = object()
    expected_runtime = object()
    app = FastAPI()
    app.state.latency_runtime = expected_runtime
    monkeypatch.setattr(runtime, "get_settings", lambda: expected_settings)
    captured = {}

    class StubExecutionService:
        def __init__(self, session, settings, *, latency_runtime):
            captured.update(session=session, settings=settings, latency_runtime=latency_runtime)

    monkeypatch.setattr(runtime, "GatewayExecutionService", StubExecutionService)
    session = object()

    runtime._gateway_execution_service(_request(app), session)

    assert captured == {
        "session": session,
        "settings": expected_settings,
        "latency_runtime": expected_runtime,
    }


@pytest.mark.asyncio
async def test_playground_request_passes_lifespan_runtime_to_gateway_service(monkeypatch):
    from src.backend_ai.routers import gateway_runtime

    expected_runtime = object()
    app = FastAPI()
    app.state.latency_runtime = expected_runtime
    captured = {}

    class StubExecutionService:
        def __init__(self, session, settings, *, latency_runtime):
            captured["latency_runtime"] = latency_runtime

        async def run_legacy(self, payload, user, *, orchestrator_builder):
            captured["payload"] = payload
            captured["user"] = user
            return {"ok": True}

    monkeypatch.setattr(gateway_runtime, "GatewayExecutionService", StubExecutionService)
    monkeypatch.setattr(gateway_runtime, "get_settings", lambda: object())
    payload, user, session = object(), object(), object()

    response = await gateway_runtime.create_request(_request(app), payload, user, session)

    assert response == {"ok": True}
    assert captured["latency_runtime"] is expected_runtime
    assert captured["payload"] is payload


@pytest.mark.asyncio
async def test_public_request_pins_latency_snapshot_for_one_request_and_resets_scope(monkeypatch):
    from src.backend_ai.services.gateway_execution_service import GatewayExecutionService

    history = HistorySnapshot(datetime.now(UTC), {})
    collector = object()
    runtime = SimpleNamespace(
        config=LatencyConfig(mode="auto"),
        model_store=SimpleNamespace(snapshot=lambda: None),
        history_cache=SimpleNamespace(snapshot=lambda: history),
        collector=collector,
    )
    settings = Settings(_env_file=None, gateway_execution_mode="live")
    service = GatewayExecutionService(None, settings, latency_runtime=runtime)
    key = ModelKey("model", "provider", "upstream", "fingerprint", "openai")
    prepared = SimpleNamespace(
        application_id="application",
        settings=settings,
        model_bindings={"model": key},
    )
    observed = {}

    async def fake_run(*args):
        observed["context"] = current_latency_context()
        return "ok"

    monkeypatch.setattr(service, "_run_public_impl", fake_run)

    result = await service.run_public(object(), object(), prepared, "request-id", None, 123.0)

    assert result == "ok"
    context = observed["context"]
    assert context.request_id == "request-id"
    assert context.mode == "auto"
    assert context.history is history
    assert context.collector is collector
    assert context.bindings["model"].digest() == key.digest()
    assert current_latency_context() is None
