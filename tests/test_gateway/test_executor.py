from dataclasses import dataclass, field

import pytest

from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.agents.routing.errors import TransientExecutionError
from src.backend_ai.agents.routing.executor import SubtaskExecutor
from src.backend_ai.agents.routing.router import ExecutionConfig
from src.backend_ai.gateway.providers.base import GenerationOptions, ProviderClient, ProviderResponse
from src.shared.dtos.gateway import Difficulty, SubTask, TaskType


class FakeRouter(BaseRouter):
    name = "fake"

    async def aroute(self, subtask, upstream_results=None, **kwargs):
        return RouteDecision(
            task_id=subtask.task_id,
            tier="primary",
            strategy=self.name,
            selected_model="primary",
            provider_name="fake",
            fallback_models=["fallback"],
            confidence=0.9,
        )


@dataclass
class FakeProvider(ProviderClient):
    name: str
    fail_once: bool = False
    prompts: list[str] = field(default_factory=list)
    model_ids: list[str] = field(default_factory=list)

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds):
        self.prompts.append(prompt)
        self.model_ids.append(model_id)
        if self.fail_once:
            self.fail_once = False
            raise TransientExecutionError("temporary provider outage")
        return ProviderResponse(
            text=f"{self.name}:{model_id}:{task_id}",
            provider=self.name,
            model_id=model_id,
            input_tokens=len(prompt.split()),
            output_tokens=1,
        )


def _task(task_id: str, *, dependencies: list[str] | None = None, input_from=None):
    return SubTask(
        task_id=task_id,
        description=f"do {task_id}",
        task_type=TaskType.GENERAL,
        difficulty=Difficulty.EASY,
        dependencies=dependencies or [],
        input_from=input_from or {},
        expected_output="text",
    )


@pytest.mark.asyncio
async def test_executor_uses_fallback_and_passes_declared_dependency_output():
    primary = FakeProvider("primary", fail_once=True)
    fallback = FakeProvider("fallback")
    executor = SubtaskExecutor(
        FakeRouter(),
        providers={"primary": primary, "fallback": fallback},
        timeout_seconds=2,
    )

    results = await executor.aexecute_subtasks(
        [_task("first"), _task("second", dependencies=["first"], input_from={"first": "summary"})]
    )

    assert results[0].status == "success"
    assert results[0].fallback is True
    assert results[0].executed_model == "fallback"
    assert results[1].status == "success"
    assert "fallback:fallback:first" in primary.prompts[-1]


@pytest.mark.asyncio
async def test_executor_marks_dependents_as_upstream_failure():
    class PermanentProvider(FakeProvider):
        async def generate(self, prompt, *, task_id, model_id, timeout_seconds):
            raise ValueError("bad request")

    executor = SubtaskExecutor(
        FakeRouter(),
        providers={"primary": PermanentProvider("primary"), "fallback": FakeProvider("fallback")},
    )

    results = await executor.aexecute_subtasks([_task("first"), _task("second", dependencies=["first"])])

    assert results[0].status == "failed"
    assert results[1].status == "failed"
    assert results[1].failure_type == "UpstreamFailure"


class CatalogRouter(BaseRouter):
    name = "catalog"

    async def aroute(self, subtask, upstream_results=None, **kwargs):
        return RouteDecision(
            task_id=subtask.task_id,
            tier="primary",
            strategy=self.name,
            selected_model="catalog-model-id",
            provider_name="openai-live",
            provider_kind="openai",
            provider_model="gpt-4o-mini",
        )


@pytest.mark.asyncio
async def test_executor_uses_catalog_model_id_and_upstream_model_name():
    provider = FakeProvider("openai")
    # The provider builder indexes live clients by catalog model ID / connection name,
    # not by provider kind (which is shared by multiple credentialed connections).
    executor = SubtaskExecutor(CatalogRouter(), providers={"catalog-model-id": provider})

    results = await executor.aexecute_subtasks([_task("catalog-task")])

    assert results[0].status == "success"
    assert results[0].executed_model == "catalog-model-id"
    assert results[0].provider == "openai"
    assert provider.model_ids == ["gpt-4o-mini"]


@pytest.mark.asyncio
async def test_executor_uses_catalog_provider_connection_and_model_name():
    provider = FakeProvider("openai")
    executor = SubtaskExecutor(CatalogRouter(), providers={"openai-live": provider})

    results = await executor.aexecute_subtasks([_task("catalog-task")])

    assert results[0].status == "success"
    assert results[0].executed_model == "catalog-model-id"
    assert results[0].provider == "openai"
    assert provider.model_ids == ["gpt-4o-mini"]


@pytest.mark.asyncio
async def test_executor_does_not_resolve_a_connection_by_its_kind():
    executor = SubtaskExecutor(CatalogRouter(), providers={"openai": FakeProvider("openai")})

    results = await executor.aexecute_subtasks([_task("catalog-task")])

    assert results[0].status == "failed"


class BareRouter(BaseRouter):
    name = "bare"

    def __init__(self, **provider):
        self._provider = provider

    async def aroute(self, subtask, upstream_results=None, **kwargs):
        return RouteDecision(
            task_id=subtask.task_id, tier="mid", strategy=self.name, selected_model="mid", **self._provider
        )


@pytest.mark.asyncio
async def test_decision_without_a_provider_runs_offline():
    offline = FakeProvider("offline")
    executor = SubtaskExecutor(BareRouter(), providers={"offline": offline})

    results = await executor.aexecute_subtasks([_task("bare-task")])

    assert results[0].status == "success"
    assert offline.model_ids == ["mid"]


@pytest.mark.asyncio
async def test_missing_named_connection_never_degrades_to_offline():
    offline = FakeProvider("offline")
    executor = SubtaskExecutor(
        BareRouter(provider_name="openai-live", provider_kind="openai"), providers={"offline": offline}
    )

    results = await executor.aexecute_subtasks([_task("named-task")])

    assert results[0].status == "failed"
    assert offline.model_ids == []


@pytest.mark.asyncio
async def test_public_executor_retries_transient_attempts_and_passes_generation_cap(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("src.backend_ai.agents.routing.executor.asyncio.sleep", no_sleep)

    class CappedProvider(ProviderClient):
        name = "capped"

        def __init__(self, failures):
            self.failures = failures
            self.options = []

        async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
            self.options.append(options)
            if self.failures:
                self.failures -= 1
                raise TransientExecutionError("temporary")
            return ProviderResponse(
                text="answer",
                provider=self.name,
                model_id=model_id,
                input_tokens=3,
                output_tokens=2,
                usage_received=True,
            )

    provider = CappedProvider(failures=2)
    executor = SubtaskExecutor(
        FakeRouter(),
        providers={"primary": provider, "fallback": CappedProvider(failures=0)},
        execution_config=ExecutionConfig(timeout_seconds=4, retry_limit=2, max_output_tokens=17),
    )
    result = (await executor.aexecute_subtasks([_task("capped")]))[0]

    assert result.status == "success"
    assert len(provider.options) == 3
    assert provider.options == [GenerationOptions(max_output_tokens=17)] * 3
    assert result.usage_complete is False  # Earlier attempts have no usage receipt.


@pytest.mark.asyncio
async def test_public_executor_falls_back_after_candidate_retry_limit(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("src.backend_ai.agents.routing.executor.asyncio.sleep", no_sleep)

    class AlwaysTransient(FakeProvider):
        async def generate(self, prompt, *, task_id, model_id, timeout_seconds, **kwargs):
            self.prompts.append(prompt)
            self.model_ids.append(model_id)
            raise TransientExecutionError("temporary")

    primary = AlwaysTransient("primary")
    fallback = FakeProvider("fallback")
    result = (
        await SubtaskExecutor(
            FakeRouter(),
            providers={"primary": primary, "fallback": fallback},
            execution_config=ExecutionConfig(timeout_seconds=4, retry_limit=1),
        ).aexecute_subtasks([_task("fallback-after-retry")])
    )[0]
    assert len(primary.model_ids) == 2
    assert result.status == "success" and result.fallback
    assert fallback.model_ids == ["fallback"]


@pytest.mark.asyncio
async def test_public_executor_does_not_start_calls_after_deadline():
    provider = FakeProvider("primary")
    result = (
        await SubtaskExecutor(
            FakeRouter(),
            providers={"primary": provider, "fallback": FakeProvider("fallback")},
            execution_config=ExecutionConfig(deadline_at=0.0),
        ).aexecute_subtasks([_task("expired")])
    )[0]
    assert result.status == "failed"
    assert "deadline" in (result.failure_reason or "").lower()
    assert provider.model_ids == []
