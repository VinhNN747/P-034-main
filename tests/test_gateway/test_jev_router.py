import importlib
import os
from types import SimpleNamespace

import pytest

from src.backend_ai.agents.routing import router as routing_module
from src.backend_ai.agents.routing.errors import TransientExecutionError
from src.backend_ai.agents.routing.executor import SubtaskExecutor
from src.backend_ai.agents.routing.router import ExecutionConfig, ExecutionResult
from src.shared.dtos.gateway import Difficulty, SubTask, TaskType


def _task() -> SubTask:
    return SubTask(
        task_id="task-1",
        description="summarize the source",
        task_type=TaskType.SUMMARIZATION,
        difficulty=Difficulty.EASY,
        expected_output="a short summary",
    )


class FakeAgent:
    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error
        self.calls = []

    async def ainvoke(self, payload):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return self.raw


class FakeFallbackModel:
    def __init__(self, content="fallback answer"):
        self.content = content
        self.calls = []

    async def ainvoke(self, message):
        self.calls.append(message)
        return SimpleNamespace(
            content=self.content,
            usage_metadata={"input_tokens": 4, "output_tokens": 2},
        )


def _raw_response():
    return {
        "model_route": SimpleNamespace(choice="small_fast", confidence=0.91),
        "messages": [
            SimpleNamespace(
                content="routed answer",
                usage_metadata={"input_tokens": 8, "output_tokens": 3},
            )
        ],
    }


def _subtask_router_class():
    return getattr(routing_module, "SubtaskRouter")


def _jev_router_class():
    return getattr(importlib.import_module("src.backend_ai.agents.routing.jev_router"), "JevRouter")


def _router(agent, fallback=None):
    return _subtask_router_class()(
        agent=agent,
        fallback_model=fallback or FakeFallbackModel(),
        model_names={"small_fast": "openai:gpt-4o-mini"},
        execution_config=ExecutionConfig(fallback_model="gpt-4o-mini", timeout_seconds=2),
    )


@pytest.mark.asyncio
async def test_subtask_router_returns_atomic_routed_execution_result():
    router = _router(FakeAgent(raw=_raw_response()))

    result = await router.aexecute_subtask(_task(), original_prompt="summarize this")

    assert result.status == "success"
    assert result.result == "routed answer"
    assert result.route == "small_fast"
    assert result.selected_model == "openai:gpt-4o-mini"
    assert result.executed_model == "openai:gpt-4o-mini"
    assert result.confidence == 0.91
    assert result.input_tokens == 8
    assert result.output_tokens == 3


@pytest.mark.asyncio
async def test_subtask_router_uses_fixed_fallback_for_transient_failure():
    agent = FakeAgent(error=TransientExecutionError("rate limited"))
    fallback = FakeFallbackModel()
    router = _router(agent, fallback)

    result = await router.aexecute_subtask(_task())

    assert result.status == "success"
    assert result.fallback is True
    assert result.executed_model == "openai:gpt-4o-mini"
    assert result.failure_type == "TransientExecutionError"
    assert len(agent.calls) == 1
    assert len(fallback.calls) == 1


@pytest.mark.asyncio
async def test_subtask_router_fails_closed_for_permanent_failure():
    agent = FakeAgent(error=ValueError("invalid request"))
    fallback = FakeFallbackModel()
    router = _router(agent, fallback)

    result = await router.aexecute_subtask(_task())

    assert result.status == "failed"
    assert result.fallback is False
    assert fallback.calls == []


class FakeSubtaskRouter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def aexecute_subtask(self, subtask, upstream_results=None, **kwargs):
        self.calls.append((subtask, upstream_results, kwargs))
        return self.result


@pytest.mark.asyncio
async def test_jev_router_adapts_prefetched_result_without_second_provider_call():
    prefetched = ExecutionResult(
        task_id="task-1",
        status="success",
        result="already executed",
        selected_model="openai:gpt-4o-mini",
        executed_model="openai:gpt-4o-mini",
        fallback=False,
        route="small_fast",
        confidence=0.9,
    )
    subtask_router = FakeSubtaskRouter(prefetched)
    router = _jev_router_class()(subtask_router=subtask_router)

    decision = await router.aroute(_task(), original_prompt="source")

    assert decision.strategy == "jev"
    assert decision.prefetched_result is prefetched
    assert decision.selected_model == "openai:gpt-4o-mini"

    class NoCallProvider:
        async def generate(self, *args, **kwargs):
            raise AssertionError("prefetched JEV result must not execute twice")

    execution_subtask_router = FakeSubtaskRouter(prefetched)
    execution_router = _jev_router_class()(subtask_router=execution_subtask_router)
    execution = await SubtaskExecutor(execution_router, providers={"offline": NoCallProvider()}).aexecute_subtask(
        _task()
    )
    assert execution is prefetched
    assert len(execution_subtask_router.calls) == 1


def test_model_choices_use_resolved_openai_key(monkeypatch):
    from src.backend_ai.agents.routing.router import _build_model_choices
    from src.shared.config import Settings

    calls = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("langchain_openai.ChatOpenAI", FakeChatOpenAI)
    settings = Settings(_env_file=None)

    _build_model_choices(5, settings=settings, api_key="resolved-openai-key")

    assert len(calls) == 3
    assert {call["api_key"] for call in calls} == {"resolved-openai-key"}


def test_subtask_router_injects_explicit_typesafe_key(monkeypatch):
    from src.backend_ai.agents.routing.router import ExecutionConfig, SubtaskRouter
    from src.shared.config import Settings

    captured = {}
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    class FakeMiddleware:
        def __init__(self, **kwargs):
            captured["environment_key"] = os.getenv("TYPESAFE_API_KEY")
            self.classifier = None

    class FakeClassifier:
        def __init__(self, *, api_key):
            captured["api_key"] = api_key

    monkeypatch.setattr(
        "src.backend_ai.agents.routing.router._build_model_choices",
        lambda timeout_seconds, *, settings, api_key, base_url=None: ({}, {}),
    )
    monkeypatch.setattr("langchain.agents.create_agent", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "langchain_typesafe.experimental.middleware.ModelRouterMiddleware",
        FakeMiddleware,
    )
    monkeypatch.setattr("langchain_typesafe.TypeSafeClassifier", FakeClassifier)

    SubtaskRouter(
        agent=None,
        fallback_model=object(),
        execution_config=ExecutionConfig(timeout_seconds=2, fallback_model="fallback"),
        settings=Settings(_env_file=None),
        openai_api_key="openai",
        typesafe_api_key="typesafe",
    )

    assert captured["environment_key"] == "typesafe"
    assert captured["api_key"] == "typesafe"
    assert os.getenv("TYPESAFE_API_KEY") is None


def test_legacy_typesafe_constructor_restores_process_env_on_failure(monkeypatch):
    from src.backend_ai.agents.routing.router import ExecutionConfig, SubtaskRouter
    from src.shared.config import Settings

    monkeypatch.setenv("TYPESAFE_API_KEY", "preexisting-value")
    monkeypatch.setattr(
        "src.backend_ai.agents.routing.router._build_model_choices",
        lambda timeout_seconds, *, settings, api_key, base_url=None: ({}, {}),
    )

    class FailingMiddleware:
        def __init__(self, **kwargs):
            assert os.environ["TYPESAFE_API_KEY"] == "resolved-typesafe-key"
            raise RuntimeError("constructor failed")

    monkeypatch.setattr(
        "langchain_typesafe.experimental.middleware.ModelRouterMiddleware",
        FailingMiddleware,
    )

    with pytest.raises(RuntimeError, match="constructor failed"):
        SubtaskRouter(
            agent=None,
            execution_config=ExecutionConfig(timeout_seconds=2, fallback_model="fallback"),
            settings=Settings(_env_file=None),
            openai_api_key="resolved-openai-key",
            typesafe_api_key="resolved-typesafe-key",
        )

    assert os.environ["TYPESAFE_API_KEY"] == "preexisting-value"
