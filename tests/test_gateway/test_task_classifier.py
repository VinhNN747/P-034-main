"""ClassifierUnderstander and the task-classifier shadow, with a fake classifier (no onnxruntime or model files)."""

import pytest

from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.routing.tier_policy import TierPolicy, TierPolicyConfig
from src.backend_ai.gateway.understanding.rules import RuleUnderstander
from src.backend_ai.gateway.understanding.task_classifier import (
    ClassifierUnderstander,
    OnnxTaskClassifier,
    TaskClassification,
)
from src.shared.dtos.gateway import (
    EffectiveConstraints,
    GatewayRequest,
    RequestConstraints,
    RoutingSignals,
    TaskType,
)


class FakeClassifier:
    def __init__(self, task_type=TaskType.CODING, probability=0.9, difficulty=0.4, error=None):
        self.result = TaskClassification(task_type, probability, difficulty, model="fake", latency_ms=1.0)
        self.error = error
        self.calls = []

    async def aclassify(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return self.result


def request(prompt, request_id="clf-1"):
    return GatewayRequest(request_id=request_id, application_id="tests", prompt=prompt)


def constraints():
    return EffectiveConstraints(constraints=RequestConstraints(), resolved={})


def understand_trace(result):
    return next(t for t in result.traces if t.stage == "understand")


@pytest.mark.asyncio
async def test_classifier_replaces_the_rule_task_type_and_scores():
    prompt = "My React component re-renders on every keystroke, can you make it faster?"
    rule = await RuleUnderstander().understand(request(prompt), constraints())
    assert TaskType.CODING not in rule.analysis.task_types  # the regex misses it

    outcome = await ClassifierUnderstander(FakeClassifier()).understand(request(prompt), constraints())

    assert outcome.strategy == "classifier"
    assert not outcome.fallback
    assert outcome.analysis.task_types == [TaskType.CODING]
    assert outcome.analysis.required_capabilities == ["code_generation"]
    assert outcome.details["task_classifier"] == {
        "task_type": "coding", "probability": 0.9, "difficulty": 0.4, "model": "fake", "latency_ms": 1.0,
    }
    signals = outcome.analysis.signals()
    assert (signals.source, signals.difficulty, signals.confidence) == ("classifier", 0.4, 0.9)
    # no regex score reaches routing, and the request is one task
    assert (signals.reasoning, signals.domain, signals.ambiguity, signals.risk, signals.output) == (0, 0, 0, 0, 0)
    assert outcome.analysis.goal_count == 1


@pytest.mark.asyncio
async def test_regex_task_types_and_scores_are_not_mixed_in():
    prompt = "Translate this legal contract's Python docstring into French, then explain why."
    rule = await RuleUnderstander().understand(request(prompt), constraints())
    assert rule.analysis.risk_level > 0 and TaskType.TRANSLATION in rule.analysis.task_types

    fake = FakeClassifier(task_type=TaskType.CONTENT_GENERATION, probability=0.3)
    outcome = await ClassifierUnderstander(fake).understand(request(prompt), constraints())

    assert outcome.analysis.task_types == [TaskType.CONTENT_GENERATION]
    assert outcome.analysis.required_capabilities == []
    assert outcome.analysis.risk_level == 0


@pytest.mark.asyncio
async def test_measured_context_and_output_format_still_set_capabilities():
    req = GatewayRequest(request_id="clf-ctx", application_id="tests", prompt="Answer from this.", context={"policy": "refunds in 30 days"})
    effective = EffectiveConstraints(constraints=RequestConstraints(output_format="json"), resolved={})

    outcome = await ClassifierUnderstander(FakeClassifier(task_type=TaskType.GENERAL)).understand(req, effective)

    assert outcome.analysis.required_capabilities == ["long_context", "structured_output"]


@pytest.mark.parametrize(
    ("difficulty", "rule", "tier"),
    [(0.1, "C1", "small_fast"), (0.22, "C1", "small_fast"), (0.23, "C2", "mid"), (0.6, "C2", "mid"),
     (0.61, "C3", "large_reasoning"), (0.95, "C3", "large_reasoning")],
)
def test_classifier_difficulty_picks_the_tier(difficulty, rule, tier):
    decision = TierPolicy().decide(_classifier_signals(difficulty), task_type=TaskType.GENERAL)

    assert (decision.base_rule, decision.tier) == (rule, tier)


def test_the_gateway_and_the_policy_share_the_calibrated_default_thresholds():
    # eval/task_type/calibrate_thresholds.py picks these for the current model; a retrain must update both.
    from src.shared.config import Settings

    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:")
    configured = (settings.gateway_classifier_mid_difficulty, settings.gateway_classifier_large_difficulty)
    assert configured == TierPolicyConfig().classifier_thresholds == (0.23, 0.61)


def test_classifier_thresholds_per_task_type():
    policy = TierPolicy(TierPolicyConfig(classifier_task_thresholds={"coding": (0.1, 0.4)}))

    assert policy.decide(_classifier_signals(0.45), task_type=TaskType.CODING).tier == "large_reasoning"
    assert policy.decide(_classifier_signals(0.45), task_type=TaskType.GENERAL).tier == "mid"


def test_unsure_classifier_lifts_one_tier_and_floors_still_apply():
    unsure = TierPolicy().decide(_classifier_signals(0.1, confidence=0.4), task_type=TaskType.GENERAL)
    assert (unsure.base_tier, unsure.tier) == ("small_fast", "mid")

    with_image = _classifier_signals(0.1).model_copy(update={"visual_attachments": 1})
    assert TierPolicy().decide(with_image, task_type=TaskType.GENERAL).tier == "mid"


def _classifier_signals(difficulty, confidence=0.9):
    return RoutingSignals(
        reasoning=0, domain=0, ambiguity=0, risk=0, output=0,
        source="classifier", difficulty=difficulty, confidence=confidence,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("classifier", "error"),
    [(None, "not configured"), (FakeClassifier(error=RuntimeError("boom")), "boom")],
)
async def test_missing_or_broken_classifier_falls_back_to_rules(classifier, error):
    outcome = await ClassifierUnderstander(classifier).understand(request("Summarize this report."), constraints())

    assert outcome.fallback
    assert outcome.strategy == "classifier->rule"
    assert error in outcome.error
    assert outcome.analysis.task_types == [TaskType.SUMMARIZATION]


def test_onnx_classifier_load_is_none_without_model_files(tmp_path):
    assert OnnxTaskClassifier.load(None) is None
    (tmp_path / "labels.json").write_text("{}", encoding="utf-8")
    assert OnnxTaskClassifier.load(tmp_path) is None  # tokenizer.json and model.onnx missing


@pytest.mark.asyncio
async def test_pipeline_logs_the_shadow_classification_without_changing_the_analysis():
    fake = FakeClassifier()
    prompt = "Summarize this report in three bullet points."

    result = await RequestPipeline(task_classifier_shadow=fake).run(request(prompt), understander="rule")

    trace = understand_trace(result)
    assert trace.strategy == "rule"
    assert trace.metadata["task_classifier_shadow"]["task_type"] == "coding"
    assert result.semantic_analysis.task_types == [TaskType.SUMMARIZATION]  # routing input unchanged
    assert fake.calls == [request(prompt).focus_text()]


@pytest.mark.asyncio
async def test_a_failing_shadow_never_fails_the_request():
    fake = FakeClassifier(error=RuntimeError("model file corrupt"))

    result = await RequestPipeline(task_classifier_shadow=fake).run(request("Summarize this."), understander="rule")

    assert result.validation.accepted
    assert understand_trace(result).metadata["task_classifier_shadow"] == {"error": "model file corrupt"}


@pytest.mark.asyncio
async def test_selected_classifier_understander_is_traced_once_without_shadow():
    fake = FakeClassifier()
    pipeline = RequestPipeline(task_classifier_shadow=fake)
    pipeline.understanders["classifier"] = ClassifierUnderstander(fake)

    result = await pipeline.run(request("Write a Go LRU cache."), understander="classifier")

    trace = understand_trace(result)
    assert trace.strategy == "classifier"
    assert trace.metadata["task_classifier"]["task_type"] == "coding"
    assert "task_classifier_shadow" not in trace.metadata
    assert len(fake.calls) == 1
    assert result.semantic_analysis.task_types == [TaskType.CODING]


@pytest.mark.asyncio
async def test_classifier_requests_are_never_decomposed():
    fake = FakeClassifier(task_type=TaskType.CONTENT_GENERATION, difficulty=0.8)
    pipeline = RequestPipeline()
    pipeline.understanders["classifier"] = ClassifierUnderstander(fake)
    prompt = "1. Write a poem about Hanoi.\n2. Translate it into English.\n3. Summarize both in one line."

    result = await pipeline.run(request(prompt, "clf-multi"), understander="classifier", decomposer="deterministic")

    [task] = result.decomposition.subtasks
    assert task.routing_signals.source == "classifier"
    assert TierPolicy().decide(task.routing_signals, task_type=task.task_type).tier == "large_reasoning"
