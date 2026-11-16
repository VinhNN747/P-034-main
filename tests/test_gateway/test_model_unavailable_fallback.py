from dataclasses import dataclass, field

import pytest

from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.agents.routing.executor import SubtaskExecutor
from src.backend_ai.agents.routing.router import ExecutionConfig
from src.backend_ai.gateway.providers import openai as openai_provider
from src.backend_ai.gateway.providers.base import (
    ModelUnavailableError,
    PermanentProviderError,
    ProviderClient,
    ProviderResponse,
)
from src.shared.dtos.gateway import Difficulty, OutputEvidence, SubTask, TaskType


class TwoModelRouter(BaseRouter):
    name = "fake"

    async def aroute(self, subtask, upstream_results=None, **kwargs):
        return RouteDecision(
            task_id=subtask.task_id,
            tier="primary",
            strategy=self.name,
            selected_model="primary",
            provider_name="primary",
            input_price_per_million=1.0,
            output_price_per_million=10.0,
            fallback_models=["fallback"],
            fallback_provider_names={"fallback": "fallback"},
            fallback_input_prices_per_million={"fallback": 1.0},
            fallback_output_prices_per_million={"fallback": 10.0},
            confidence=0.9,
        )


@dataclass
class ScriptedProvider(ProviderClient):
    name: str
    error: Exception | None = None
    text: str = "answer"
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    calls: list[str] = field(default_factory=list)

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        self.calls.append(model_id)
        if self.error is not None:
            raise self.error
        return ProviderResponse(
            text=self.text,
            provider=self.name,
            model_id=model_id,
            input_tokens=100,
            output_tokens=800,
            usage_received=True,
            output_evidence=OutputEvidence(
                finish_reason=self.finish_reason or ("length" if not self.text else "stop"),
                reasoning_tokens=self.reasoning_tokens,
            ),
        )


def _task():
    return SubTask(task_id="t", description="do it", task_type=TaskType.GENERAL, difficulty=Difficulty.EASY,
                   expected_output="text")


async def _run(primary: ScriptedProvider, fallback: ScriptedProvider):
    executor = SubtaskExecutor(
        TwoModelRouter(),
        providers={"primary": primary, "fallback": fallback},
        execution_config=ExecutionConfig(timeout_seconds=2, retry_limit=2),
    )
    return (await executor.aexecute_subtasks([_task()]))[0]


@pytest.mark.asyncio
async def test_missing_model_moves_to_the_next_candidate_without_retrying():
    primary = ScriptedProvider("primary", error=ModelUnavailableError("model_not_found", status_code=404))
    fallback = ScriptedProvider("fallback")
    result = await _run(primary, fallback)
    assert result.status == "success"
    assert result.executed_model == "fallback" and result.fallback is True
    assert primary.calls == ["primary"]  # no retries against a model that does not exist


@pytest.mark.asyncio
async def test_other_permanent_errors_still_stop_the_request():
    primary = ScriptedProvider("primary", error=PermanentProviderError("invalid request", status_code=400))
    fallback = ScriptedProvider("fallback")
    result = await _run(primary, fallback)
    assert result.status == "failed"
    assert fallback.calls == []


@pytest.mark.asyncio
async def test_empty_output_falls_back_and_keeps_the_wasted_cost():
    primary = ScriptedProvider("primary", text="")
    fallback = ScriptedProvider("fallback", text="real answer")
    result = await _run(primary, fallback)
    assert result.status == "success" and result.result == "real answer"
    assert result.executed_model == "fallback"
    # Both attempts were billed: 2 x (100 in at $1/M + 800 out at $10/M).
    assert result.input_tokens == 200 and result.output_tokens == 1600
    assert result.cost_usd == pytest.approx(2 * (100 / 1e6 * 1.0 + 800 / 1e6 * 10.0))
    assert "no visible output" in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_all_candidates_empty_fails_with_cost_reported():
    result = await _run(ScriptedProvider("primary", text=""), ScriptedProvider("fallback", text=""))
    assert result.status == "failed"
    assert result.failure_type == "ModelUnavailableError"
    assert result.cost_usd == pytest.approx(2 * (100 / 1e6 * 1.0 + 800 / 1e6 * 10.0))


@pytest.mark.asyncio
async def test_truncated_thinking_fragment_falls_back():
    # 800 output tokens, 770 of them reasoning: the "answer" is a thought fragment.
    primary = ScriptedProvider("primary", text="Tone: professional...", finish_reason="length", reasoning_tokens=770)
    fallback = ScriptedProvider("fallback", text="real answer")
    result = await _run(primary, fallback)
    assert result.result == "real answer" and result.executed_model == "fallback"


@pytest.mark.asyncio
async def test_long_answer_cut_off_near_its_end_is_kept():
    primary = ScriptedProvider("primary", text="a long answer...", finish_reason="length", reasoning_tokens=200)
    result = await _run(primary, ScriptedProvider("fallback"))
    assert result.result == "a long answer..." and result.executed_model == "primary"


def test_reasoning_models_get_headroom_and_low_effort(monkeypatch):
    created = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)

    import langchain_openai

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    provider = openai_provider.OpenAIProvider(enabled=True, api_key="sk-test")
    provider._client_for("gpt-5-mini", 30, 800)
    provider._client_for("gpt-4o-mini", 30, 800)
    reasoning, plain = created
    assert reasoning["reasoning_effort"] == "low"
    assert reasoning["max_tokens"] == 800 + openai_provider.REASONING_TOKEN_HEADROOM
    assert "reasoning_effort" not in plain and plain["max_tokens"] == 800


def test_404_is_classified_as_model_unavailable():
    class NotFoundError(Exception):
        status_code = 404

    assert openai_provider._is_model_unavailable(NotFoundError("The model `gpt-6-luna-pro` does not exist"))
    assert not openai_provider._is_model_unavailable(type("Bad", (Exception,), {"status_code": 400})("bad"))
