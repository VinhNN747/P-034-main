from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.backend_ai.latency.context import LatencyRequestContext, latency_request_scope
from src.backend_ai.latency.contracts import HistorySnapshot, ModelKey


class StopAfterRouteError(Exception):
    pass


class Message:
    tool_calls = None
    tool_call_id = None
    refusal = None

    def model_dump(self, **_kwargs):
        return {"role": "user", "content": "classify this"}


@pytest.mark.asyncio
async def test_openai_sdk_message_route_records_objective_and_protocol_features(monkeypatch):
    from src.backend_ai.gateway.decomposition.no_decomposer import NoDecomposer
    from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
    from src.backend_ai.services.openai_message_execution import execute_openai_messages

    key = ModelKey("model", "provider", "upstream", "fingerprint", "openai")
    now = datetime.now(UTC)
    context = LatencyRequestContext(
        request_id="request",
        source="openai_chat_completions",
        mode="auto",
        artifact=None,
        history=HistorySnapshot(now, {}),
        collector=None,
        bindings={"model": key},
    )
    task = SimpleNamespace(task_id="task", task_type="classification", routing_signals=None)
    result = SimpleNamespace(
        semantic_analysis=object(),
        profile=object(),
        effective_constraints=SimpleNamespace(
            constraints=SimpleNamespace(max_output_tokens=128, quality_priority="latency_first")
        ),
        goal_extraction=object(),
        attachments=None,
        decomposition=None,
    )

    async def decompose(*_args):
        return SimpleNamespace(result=SimpleNamespace(subtasks=[task]))

    monkeypatch.setattr(NoDecomposer, "decompose", decompose)
    monkeypatch.setattr(RequestPipeline, "_attach_routing_signals", lambda *_args: SimpleNamespace(subtasks=[task]))

    class Router:
        async def aroute(self, _task, **kwargs):
            self.features = kwargs["latency_features"]
            raise StopAfterRouteError

    router = Router()
    payload = SimpleNamespace(
        tools=None,
        n=1,
        response_format=None,
        messages=[Message()],
        temperature=None,
        top_p=None,
        stop=None,
        seed=None,
        frequency_penalty=None,
        presence_penalty=None,
        parallel_tool_calls=None,
        tool_choice=None,
    )
    prepared = SimpleNamespace(
        application_policy=SimpleNamespace(max_output_tokens=512),
        providers={},
        config=SimpleNamespace(timeout_seconds=10),
    )
    request = SimpleNamespace(focus_text=lambda: "classify this", request_id="request")

    with latency_request_scope(context), pytest.raises(StopAfterRouteError):
        await execute_openai_messages(request, result, payload, prepared, router, 10**9)

    features = router.features["model"]
    assert features.values["protocol"] == "openai_sdk"
    assert features.values["routing_objective"] == "latency_first"
