"""difficulty_gap mode: a request is split only when the split gate sees parts of very
different difficulty AND jev scores the subtasks at least ``split_min_tier_gap`` tiers apart."""

import pytest

from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.scoring.decomposition_policy import DecompositionPolicy
from src.backend_ai.gateway.understanding.jev import JevDecision, JevUnderstander
from src.backend_ai.gateway.understanding.rules import RuleUnderstander
from src.backend_ai.gateway.understanding.split_gate import SplitGate, SplitGateDecision
from src.shared.dtos.gateway import GoalExtractionResult, ModelUsage, RequestProfile, TaskType
from tests.test_gateway.test_tier_policy import _request

NUMBERED = "1. Translate 'good morning' to French\n2. Prove that there are infinitely many primes"
NUMBERED_EASY = "1. Translate 'good morning' to French\n2. Translate 'thank you' to German"


class FakeJev:
    """Scores 'prove' as deep reasoning (large_reasoning), anything else as trivial (small_fast)."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.prompts: list[str] = []
        self.fail_on = fail_on

    async def decide(self, prompt: str) -> JevDecision:
        self.prompts.append(prompt)
        if self.fail_on and self.fail_on in prompt and "\n\nExpected output:" in prompt:
            raise RuntimeError("jev down")
        hard = "Prove" in prompt or "prove" in prompt
        return JevDecision(
            is_multi_task=False,
            task_type=TaskType.GENERAL,
            reasoning_depth=3 if hard else 0,
            domain_specialization=0,
            ambiguity=0,
            risk=0,
            output_complexity=0,
            confidence=1.0,
            usage=ModelUsage(input_tokens=100),
        )


class FakeGate:
    def __init__(self, probability: float | None) -> None:
        self.probability = probability
        self.calls = 0

    async def check(self, prompt: str) -> SplitGateDecision:
        self.calls += 1
        if self.probability is None:
            raise RuntimeError("gate down")
        return SplitGateDecision(gap_probability=self.probability, usage=ModelUsage(input_tokens=50))


def _pipeline(gate_probability, jev: FakeJev | None = None, **config):
    jev = jev or FakeJev()
    gate = FakeGate(gate_probability)
    pipeline = RequestPipeline(
        config=GatewayConfig(**config),
        understanders={"rule": RuleUnderstander(), "jev": JevUnderstander(jev)},
        split_gate=SplitGate(gate),
    )
    return pipeline, gate, jev


def _trace(result, stage):
    return next((trace for trace in result.traces if trace.stage == stage), None)


@pytest.mark.asyncio
async def test_parts_of_very_different_difficulty_are_split_and_keep_their_own_tiers():
    pipeline, gate, _ = _pipeline(0.9)
    result = await pipeline.run(_request(NUMBERED), understander="jev")

    assert gate.calls == 1
    tasks = result.decomposition.subtasks
    assert len(tasks) == 2
    # Each subtask carries its own jev scores, not the request's capped ones.
    assert [task.routing_signals.reasoning for task in tasks] == [0, 3]
    scoring = _trace(result, "decompose_scoring")
    assert scoring.metadata["split_kept"] is True
    assert scoring.metadata["tier_gap"] == 2
    assert sorted(scoring.metadata["tiers"].values()) == ["large_reasoning", "small_fast"]
    assert _trace(result, "decomposition_decision").metadata["band"] == "gap-deterministic"
    # Gate and subtask scoring are paid for as part of decomposition.
    assert result.costs.decomposition_cost == pytest.approx(
        sum(t.cost_usd for t in result.traces if t.stage.startswith("decompose"))
    )


@pytest.mark.asyncio
async def test_parts_in_the_same_tier_are_folded_back_into_one_task():
    pipeline, _, _ = _pipeline(0.9)
    result = await pipeline.run(_request(NUMBERED_EASY), understander="jev")

    assert len(result.decomposition.subtasks) == 1
    scoring = _trace(result, "decompose_scoring")
    assert scoring.metadata["split_kept"] is False
    assert scoring.metadata["tier_gap"] == 0


@pytest.mark.asyncio
async def test_a_one_step_gap_is_not_enough_by_default_but_is_when_configured():
    # mid sits one step above small_fast: reasoning 2 -> R3 mid with the default thresholds.
    class MidJev(FakeJev):
        async def decide(self, prompt):
            decision = await super().decide(prompt)
            return decision.model_copy(update={"reasoning_depth": 2.0}) if "Prove" in prompt else decision

    for min_gap, expected in ((2, 1), (1, 2)):
        pipeline, _, _ = _pipeline(0.9, MidJev(), split_min_tier_gap=min_gap)
        result = await pipeline.run(_request(NUMBERED), understander="jev")
        assert len(result.decomposition.subtasks) == expected, min_gap


@pytest.mark.asyncio
async def test_low_gate_probability_keeps_one_task_without_scoring_subtasks():
    pipeline, gate, jev = _pipeline(0.1)
    result = await pipeline.run(_request(NUMBERED), understander="jev")

    assert gate.calls == 1
    assert len(result.decomposition.subtasks) == 1
    assert _trace(result, "decomposition_decision").metadata["band"] == "gap-no"
    assert _trace(result, "decompose_scoring") is None
    assert len(jev.prompts) == 1  # only the understand call


@pytest.mark.asyncio
async def test_gate_failure_means_one_task():
    pipeline, _, _ = _pipeline(None)
    result = await pipeline.run(_request(NUMBERED), understander="jev")

    assert len(result.decomposition.subtasks) == 1
    gate = _trace(result, "decompose_gate")
    assert gate.status == "fallback" and "gate down" in gate.error
    assert _trace(result, "decomposition_decision").metadata["band"] == "gap-unavailable"


@pytest.mark.asyncio
async def test_a_subtask_jev_cannot_score_folds_the_split():
    pipeline, _, _ = _pipeline(0.9, FakeJev(fail_on="primes"))
    result = await pipeline.run(_request(NUMBERED), understander="jev")

    assert len(result.decomposition.subtasks) == 1
    scoring = _trace(result, "decompose_scoring")
    assert scoring.status == "fallback" and "jev down" in scoring.error


@pytest.mark.asyncio
async def test_without_a_split_gate_nothing_is_split():
    result = await RequestPipeline().run(_request(NUMBERED))
    assert len(result.decomposition.subtasks) == 1
    assert _trace(result, "decompose_gate") is None


@pytest.mark.asyncio
async def test_latency_first_callers_skip_the_gate():
    pipeline, gate, _ = _pipeline(0.9)
    request = _request(NUMBERED)
    request.metadata["disable_decomposition"] = True
    result = await pipeline.run(request, understander="jev")
    assert gate.calls == 0
    assert len(result.decomposition.subtasks) == 1


def _policy_decide(probability, decision="multi", goals=2, planner=False):
    extraction = GoalExtractionResult.model_construct(
        decision=decision,
        goals=[type("Goal", (), {"independently_executable": True})() for _ in range(goals)],
    )
    profile = RequestProfile.model_construct(ambiguity_score=0.0, decomposition_score=0.0)
    return DecompositionPolicy(GatewayConfig()).decide(
        _request("x"),
        profile,
        10,
        goal_extraction=extraction,
        planner_available=planner,
        split_gap_probability=probability,
    )


@pytest.mark.parametrize(
    ("probability", "decision", "goals", "planner", "band", "split"),
    [
        (None, "multi", 2, False, "gap-unavailable", False),
        (0.49, "multi", 2, True, "gap-no", False),
        (0.5, "single", 1, True, "gap-planner", True),
        (0.8, "multi", 2, False, "gap-deterministic", True),
        (0.8, "unclear", 2, False, "gap-candidates", True),
        (0.8, "unclear", 1, False, "gap-no-boundaries", False),
        (0.8, "single", 1, False, "gap-no-boundaries", False),
    ],
)
def test_policy_bands(probability, decision, goals, planner, band, split):
    result = _policy_decide(probability, decision, goals, planner)
    assert (result.band, result.should_decompose) == (band, split)
