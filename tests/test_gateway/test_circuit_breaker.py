import pytest

from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.agents.routing.deterministic_router import select_tier
from src.backend_ai.agents.routing.executor import SubtaskExecutor
from src.backend_ai.gateway.core.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState
from src.backend_ai.gateway.providers.base import (
    PermanentProviderError,
    ProviderClient,
    ProviderResponse,
    TransientProviderError,
)
from src.shared.dtos.gateway import AttachmentKind, Difficulty, QualityPriority, SubTask, TaskType


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _breaker(clock, **overrides):
    config = CircuitBreakerConfig(**{"failure_threshold": 3, "window_seconds": 60, "open_seconds": 10, **overrides})
    return CircuitBreaker(config, clock=clock)


def test_opens_after_threshold_then_half_open_probe_closes_it():
    clock = Clock()
    breaker = _breaker(clock)
    for _ in range(2):
        breaker.record_failure("m")
    assert breaker.allow("m") and breaker.state("m") == CircuitState.CLOSED
    breaker.record_failure("m")
    assert breaker.state("m") == CircuitState.OPEN
    assert not breaker.allow("m")

    clock.now += 10
    assert breaker.allow("m")  # the single probe
    assert not breaker.allow("m")  # no second concurrent probe
    breaker.record_success("m")
    assert breaker.state("m") == CircuitState.CLOSED


def test_failed_probe_reopens_with_exponential_backoff_capped():
    clock = Clock()
    breaker = _breaker(clock, max_open_seconds=25)
    for _ in range(3):
        breaker.record_failure("m")
    clock.now += 10
    assert breaker.allow("m")
    breaker.record_failure("m")  # probe failed: open for 20s now
    clock.now += 15
    assert not breaker.allow("m")
    clock.now += 5
    assert breaker.allow("m")
    breaker.record_failure("m")  # would be 40s, capped at 25s
    clock.now += 24
    assert not breaker.allow("m")
    clock.now += 1
    assert breaker.allow("m")


def test_failures_outside_window_do_not_accumulate():
    clock = Clock()
    breaker = _breaker(clock)
    breaker.record_failure("m")
    breaker.record_failure("m")
    clock.now += 61
    breaker.record_failure("m")
    assert breaker.state("m") == CircuitState.CLOSED


def test_retry_after_opens_immediately_for_that_long():
    clock = Clock()
    breaker = _breaker(clock)
    breaker.record_failure("m", retry_after=42)
    assert breaker.state("m") == CircuitState.OPEN
    [row] = breaker.snapshot()
    assert row["retry_in_seconds"] == 42


def test_quality_priority_nudges_deterministic_tier():
    task = SubTask(
        task_id="t", description="d", task_type=TaskType.GENERAL, difficulty=Difficulty.MEDIUM, expected_output="x"
    )
    hard = task.model_copy(update={"difficulty": Difficulty.HARD})
    assert select_tier(task) == "mid"
    assert select_tier(task, QualityPriority.COST_FIRST) == "small_fast"
    assert select_tier(task, QualityPriority.QUALITY_FIRST) == "large_reasoning"
    assert select_tier(hard, QualityPriority.COST_FIRST) == "large_reasoning"


class Router(BaseRouter):
    async def aroute(self, subtask, upstream_results=None, **kwargs):
        return RouteDecision(
            task_id=subtask.task_id,
            tier="mid",
            strategy="test",
            selected_model="primary",
            fallback_models=["backup"],
        )


class FlakyProvider(ProviderClient):
    name = "flaky"

    def __init__(self, error):
        self.error = error
        self.calls: list[str] = []

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds):
        self.calls.append(model_id)
        if model_id == "primary":
            raise self.error
        return ProviderResponse(text=f"ok:{model_id}", provider=self.name, model_id=model_id)


def _task(task_id="t1"):
    return SubTask(
        task_id=task_id, description="d", task_type=TaskType.GENERAL, difficulty=Difficulty.EASY, expected_output="x"
    )


def _executor(provider, breaker):
    return SubtaskExecutor(
        Router(), providers={"primary": provider, "backup": provider}, timeout_seconds=1, circuit_breaker=breaker
    )


@pytest.mark.asyncio
async def test_executor_stops_calling_a_failing_model_once_its_circuit_opens():
    provider = FlakyProvider(TransientProviderError("503 upstream"))
    breaker = _breaker(Clock())
    executor = _executor(provider, breaker)
    for index in range(3):
        result = await executor.aexecute_subtask(_task(f"t{index}"))
        assert result.status == "success" and result.executed_model == "backup"
    assert provider.calls.count("primary") == 3

    result = await executor.aexecute_subtask(_task("t-after"))
    assert result.executed_model == "backup"
    assert result.circuit_skipped == ["primary"]
    assert provider.calls.count("primary") == 3  # no fourth attempt / timeout wait


@pytest.mark.asyncio
async def test_permanent_errors_do_not_trip_the_breaker():
    provider = FlakyProvider(PermanentProviderError("400 bad request", status_code=400))
    breaker = _breaker(Clock())
    executor = _executor(provider, breaker)
    for index in range(5):
        result = await executor.aexecute_subtask(_task(f"t{index}"))
        assert result.status == "failed"
    assert breaker.state("primary") == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_all_candidates_open_fails_fast_with_circuit_open():
    breaker = _breaker(Clock())
    for key in ("primary", "backup"):
        breaker.record_failure(key, retry_after=30)
    provider = FlakyProvider(TransientProviderError("x"))
    result = await _executor(provider, breaker).aexecute_subtask(_task())
    assert result.status == "failed"
    assert result.failure_type == "CircuitOpen"
    assert result.circuit_skipped == ["primary", "backup"]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_ocr_breaker_skips_gemini_while_open():
    from src.backend_ai.gateway.multimodal.analyzer import AttachmentAnalyzer
    from tests.test_gateway.test_multimodal import _image_part, _request

    class ReadTimeoutError(Exception):
        pass

    class BrokenOcr:
        provider = "google"
        model = "gemini-2.5-flash"
        calls = 0

        async def ocr(self, data, mime_type, *, kind):
            BrokenOcr.calls += 1
            raise ReadTimeoutError("read timeout")

    breaker = _breaker(Clock(), failure_threshold=2)
    analyzer = AttachmentAnalyzer(BrokenOcr(), circuit_breaker=breaker)
    for _ in range(2):
        [item] = await analyzer.analyze(_request(_image_part()))
        assert item.status == "failed"
    [item] = await analyzer.analyze(_request(_image_part()))
    assert item.status == "skipped" and "circuit open" in item.error
    assert item.kind == AttachmentKind.IMAGE
    assert BrokenOcr.calls == 2
