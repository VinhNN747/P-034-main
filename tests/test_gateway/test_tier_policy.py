from types import SimpleNamespace

import pytest

from src.backend_ai.agents.routing.deterministic_router import DeterministicRouter
from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.aggregation.no_aggregator import NoAggregator
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.backend_ai.gateway.routing.tier_policy import TierPolicy, TierPolicyConfig
from src.backend_ai.gateway.understanding.jev import SCORE_RUBRICS, JevUnderstander, TypeSafeJevClient
from src.backend_ai.gateway.understanding.rules import RuleUnderstander
from src.shared.dtos.gateway import (
    DecompositionResult,
    Difficulty,
    GatewayRequest,
    QualityPriority,
    RequestConstraints,
    RoutingSignals,
    SemanticAnalysis,
    SubTask,
    TaskType,
)


def _signals(reasoning=0, domain=0, ambiguity=0, risk=0, output=0, confidence=None, rarity=0, input_chars=0, live=0):
    return RoutingSignals(
        reasoning=reasoning, domain=domain, ambiguity=ambiguity, risk=risk, output=output, confidence=confidence,
        knowledge_rarity=rarity, input_chars=input_chars, live_data=live,
    )


@pytest.mark.parametrize(
    ("signals", "task_type", "tier", "rule"),
    [
        (_signals(reasoning=2.7), TaskType.GENERAL, "large_reasoning", "R1"),
        (_signals(risk=2.8, domain=2.8), TaskType.GENERAL, "large_reasoning", "R2"),
        (_signals(risk=2.7, ambiguity=1.9), TaskType.GENERAL, "large_reasoning", "R2"),
        # R2 needs serious risk (r2_min_risk 2.7): ordinary money/health/legal questions stay mid.
        (_signals(risk=2.4, domain=2.8), TaskType.GENERAL, "mid", "R4"),
        (_signals(risk=2.0, ambiguity=1.9), TaskType.GENERAL, "mid", "R4"),
        # Live data (stock picks, news): risk cannot buy a better answer; analysis still lifts.
        (_signals(reasoning=0.1, domain=1.0, risk=2.8, live=0.9), TaskType.GENERAL, "small_fast", "R6"),
        (_signals(reasoning=2.4, domain=2.0, risk=2.9, live=0.8), TaskType.GENERAL, "mid", "R3"),
        (_signals(reasoning=0.1, domain=1.0, risk=2.8, live=0.4), TaskType.GENERAL, "large_reasoning", "R2b"),
        (_signals(risk=2.9, domain=1.3), TaskType.GENERAL, "large_reasoning", "R2b"),
        # R3 adds reasoning + 1.4*domain + output (threshold 5.0): several
        # moderate dimensions lift, one high dimension alone does not.
        (_signals(reasoning=2.0, domain=1.5, output=1.0), TaskType.GENERAL, "mid", "R3"),
        (_signals(reasoning=1.0, domain=2.0, output=2.0), TaskType.GENERAL, "mid", "R3"),
        (_signals(reasoning=1.5, domain=1.5, output=1.0), TaskType.GENERAL, "small_fast", "R6"),
        (_signals(domain=2.8), TaskType.GENERAL, "small_fast", "R6"),
        # V5 thresholds: jev's 1.5-1.8 scores no longer count as high.
        (_signals(reasoning=1.8, risk=1.8, ambiguity=1.8), TaskType.GENERAL, "small_fast", "R6"),
        # ...except reasoning: math/logic puzzles score high on it alone.
        (_signals(reasoning=2.6), TaskType.GENERAL, "mid", "R3"),
        (_signals(reasoning=2.0), TaskType.GENERAL, "mid", "R3"),
        (_signals(reasoning=1.9), TaskType.GENERAL, "small_fast", "R6"),
        (_signals(output=2.0), TaskType.GENERAL, "small_fast", "R6"),
        (_signals(risk=2.0), TaskType.GENERAL, "mid", "R4"),
        (_signals(reasoning=1.0), TaskType.CODING, "mid", "R5"),
        (_signals(reasoning=1.4, domain=1.4), TaskType.TRANSLATION, "small_fast", "R6"),
        # Short obscure-fact questions: rarity alone lifts the tier to mid, never higher.
        (_signals(reasoning=0.3, rarity=2.9), TaskType.CLASSIFICATION, "mid", "R3r"),
        (_signals(reasoning=0.3, rarity=2.0), TaskType.CLASSIFICATION, "mid", "R3r"),
        (_signals(reasoning=0.3, rarity=1.8), TaskType.CLASSIFICATION, "small_fast", "R6"),
        # Long reading material alone no longer lifts the tier: size is a capacity check
        # against each model's context window when the model is chosen (R0c is off by default).
        (_signals(reasoning=0.5, input_chars=5000), TaskType.GENERAL, "small_fast", "R6"),
    ],
)
def test_rule_table(signals, task_type, tier, rule):
    decision = TierPolicy().decide(signals, task_type=task_type)
    assert (decision.tier, decision.rule) == (tier, rule)
    assert decision.explain().startswith(f"{rule} -> {tier}")


@pytest.mark.parametrize(
    ("signals", "priority", "tier", "base_tier", "adjustments"),
    [
        # R1 keeps its tier under cost/latency objectives (a weaker tier fails it).
        (_signals(reasoning=2.9), QualityPriority.COST_FIRST, "large_reasoning", "large_reasoning", []),
        (_signals(reasoning=2.9), QualityPriority.LATENCY_FIRST, "large_reasoning", "large_reasoning", []),
        # Below very_high the objective still steps down.
        (_signals(reasoning=2.2, domain=2.2), QualityPriority.COST_FIRST, "small_fast", "mid", ["A1 cost_first: -1"]),
        (_signals(), QualityPriority.QUALITY_FIRST, "mid", "small_fast", ["A2 quality_first: +1"]),
    ],
)
def test_objective_guards_and_recorded_adjustments(signals, priority, tier, base_tier, adjustments):
    decision = TierPolicy().decide(signals, quality_priority=priority)
    assert (decision.tier, decision.base_tier, decision.adjustments) == (tier, base_tier, adjustments)


def test_explain_shows_the_base_rule_when_the_tier_moved():
    moved = TierPolicy().decide(_signals(), quality_priority=QualityPriority.QUALITY_FIRST)
    assert moved.rule == "R6" and moved.base_rule == "R6"
    assert moved.explain().startswith("R6 -> mid (base R6 -> small_fast): no dimension is high; quality_first")
    unchanged = TierPolicy().decide(_signals(reasoning=2.2, domain=2.2))
    assert unchanged.explain().startswith("R3 -> mid: ")


def test_rarity_rule_can_be_switched_off_and_the_long_input_floor_is_opt_in():
    off = TierPolicy(TierPolicyConfig(use_knowledge_rarity=False))
    assert off.decide(_signals(rarity=3.0)).tier == "small_fast"
    # R0c is off by default: a long input alone stays small (size is checked against context windows).
    assert TierPolicy().decide(_signals(input_chars=50_000)).tier == "small_fast"
    # Re-enabled, it floors at mid, holds under cost_first like the attachment floors, and is recorded.
    floored = TierPolicy(TierPolicyConfig(long_input_chars=3000))
    assert floored.decide(_signals(input_chars=2999)).tier == "small_fast"
    long_cheap = floored.decide(_signals(reasoning=2.2, domain=2.2, input_chars=9000),
                                quality_priority=QualityPriority.COST_FIRST)
    assert long_cheap.tier == "mid" and "R0c" in long_cheap.explain()
    assert long_cheap.adjustments == ["A1 cost_first: -1", "R0c floor: mid"]


def test_mid_score_threshold_zero_restores_the_any_dimension_rule():
    old = TierPolicy(TierPolicyConfig(mid_score_threshold=0))
    assert old.decide(_signals(domain=2.0)).tier == "mid"
    assert old.decide(_signals(rarity=2.0)).rule == "R3"
    assert TierPolicy().decide(_signals(domain=2.0)).tier == "small_fast"
    assert TierPolicy(TierPolicyConfig(mid_reasoning_alone=0)).decide(_signals(reasoning=2.5)).tier == "small_fast"


def test_pipeline_measures_latest_request_not_earlier_turns():
    request = GatewayRequest(request_id="r", application_id="a", messages=[
        {"role": "user", "content": "x" * 10_000},
        {"role": "assistant", "content": "y" * 10_000},
        {"role": "user", "content": "ok, thanks"},
    ])
    task = SubTask(task_id="t", description="d", task_type=TaskType.GENERAL, difficulty=Difficulty.EASY,
                   expected_output="text")
    analysis = SemanticAnalysis(goal_count=1, task_types=[TaskType.GENERAL], reasoning_depth=0, task_independence=0,
                                dependency_clarity=3, domain_specialization=0, output_complexity=0, ambiguity_level=0)
    result = RequestPipeline._attach_routing_signals(
        DecompositionResult(
            original_request_id="r", decomposition_applied=False, subtasks=[task], execution_mode="single",
            decomposition_reason="single goal",
        ),
        analysis, [], request,
    )
    signals = result.subtasks[0].routing_signals
    assert signals.input_chars == len("ok, thanks")
    assert TierPolicy().decide(signals).tier == "small_fast"


def test_adjustments_quality_priority_and_confidence():
    policy = TierPolicy()
    mid = _signals(reasoning=2.2, domain=2.2)
    assert policy.decide(mid, quality_priority=QualityPriority.COST_FIRST).tier == "small_fast"
    assert policy.decide(mid, quality_priority=QualityPriority.QUALITY_FIRST).tier == "large_reasoning"

    risky = _signals(risk=2.0)
    decision = policy.decide(risky, quality_priority=QualityPriority.COST_FIRST)
    assert decision.tier == "mid"
    assert "cost_first ignored: risk is high" in decision.reasons

    unsure = _signals(confidence=0.4)
    # A3 is off by default (V5) and still available through min_confidence.
    assert policy.decide(unsure).tier == "small_fast"
    decision = TierPolicy(TierPolicyConfig(min_confidence=0.6)).decide(unsure)
    assert decision.tier == "mid" and "low confidence" in decision.explain()


class FakeClassifier:
    """Mimics TypeSafeClassifier.ainvoke's ClassifierResponse."""

    def __init__(self, scores, task_type="reasoning", multi=0.1, confidence=0.9, error=None, live=0.05, extra=None):
        self.scores, self.task_type, self.multi, self.confidence, self.error = scores, task_type, multi, confidence, error
        self.live = live
        self.extra = extra or {}  # the newer yes/no questions, e.g. {"competition_level": 0.9}
        self.calls = []

    async def ainvoke(self, payload):
        self.calls.append(payload)
        if self.error:
            raise self.error
        return SimpleNamespace(
            model="jev-2026-09",
            scores={
                key: SimpleNamespace(score=self.scores.get(key, 0.0), confidence=self.confidence)
                for key in SCORE_RUBRICS
            },
            choices={"task_type": SimpleNamespace(choice=self.task_type, confidence=0.95)},
            nouls={"is_multi_task": SimpleNamespace(noul=self.multi), "needs_live_data": SimpleNamespace(noul=self.live)}
            | {key: SimpleNamespace(noul=value) for key, value in self.extra.items()},
            usage=SimpleNamespace(input_tokens=120, output_tokens=8),
        )


def _request(prompt="Prove that the sum of two even numbers is even.", **kwargs):
    return GatewayRequest(request_id="r", application_id="a", prompt=prompt, **kwargs)


@pytest.mark.asyncio
async def test_jev_client_asks_all_questions_in_one_call_and_maps_scores():
    classifier = FakeClassifier({"reasoning_depth": 2.8, "risk": 0.4}, confidence=0.82)
    decision = await TypeSafeJevClient(classifier=classifier).decide("prompt")
    [call] = classifier.calls
    assert set(call["questions"]) == {
        *SCORE_RUBRICS, "task_type", "is_multi_task", "needs_live_data",
        "case_specific_high_stakes", "competition_level", "small_model_trap", "answer_in_context",
    }
    assert decision.reasoning_depth == 2.8 and decision.task_type == TaskType.REASONING
    assert decision.live_data_probability == 0.05
    assert decision.confidence == 0.82 and not decision.is_multi_task
    assert (decision.usage.input_tokens, decision.usage.output_tokens) == (120, 8)
    # A response without the newer questions leaves them unmeasured, not zero.
    assert decision.case_specific_probability is None and decision.small_model_trap_probability is None


@pytest.mark.asyncio
async def test_jev_yes_no_signals_reach_the_routing_signals():
    classifier = FakeClassifier(
        {"risk": 2.9, "domain": 2.8},
        extra={"case_specific_high_stakes": 0.1, "competition_level": 0.05, "small_model_trap": 0.2},
    )
    pipeline = RequestPipeline(understanders={"rule": RuleUnderstander(), "jev": JevUnderstander(
        TypeSafeJevClient(classifier=classifier))})
    result = await pipeline.run(_request(), understander="jev")
    signals = result.decomposition.subtasks[0].routing_signals
    assert (signals.case_specific, signals.competition, signals.small_model_trap) == (0.1, 0.05, 0.2)


def test_case_specific_gate_keeps_general_sensitive_questions_on_mid():
    policy = TierPolicy()
    general = policy.decide(RoutingSignals(reasoning=1, domain=2.8, ambiguity=0, risk=2.9, output=0, case_specific=0.1))
    assert (general.tier, general.rule) == ("mid", "R4")
    concrete = policy.decide(RoutingSignals(reasoning=1, domain=2.8, ambiguity=0, risk=2.9, output=0, case_specific=0.8))
    assert (concrete.tier, concrete.rule) == ("large_reasoning", "R2")
    # Not measured (rule scores, older jev): R2 behaves as before.
    unknown = policy.decide(RoutingSignals(reasoning=1, domain=2.8, ambiguity=0, risk=2.9, output=0))
    assert unknown.rule == "R2"


def test_answer_in_context_turns_a_risky_lookup_into_mid():
    policy = TierPolicy()
    risky = {"reasoning": 0.8, "domain": 2.9, "ambiguity": 0, "risk": 2.9, "output": 0, "knowledge_rarity": 1.8,
             "case_specific": 0.8}
    # Same question: without the document it needs the strongest tier (R2) ...
    assert policy.decide(RoutingSignals(**risky, answer_in_context=0.1)).rule == "R2"
    # ... with the rule supplied in the system prompt it is a lookup.
    grounded = policy.decide(RoutingSignals(**risky, answer_in_context=0.9))
    assert (grounded.tier, grounded.rule) == ("mid", "R4c")
    # Real reasoning over provided material still goes by the usual rules.
    analysis = policy.decide(RoutingSignals(**(risky | {"reasoning": 2.8}), answer_in_context=0.9))
    assert analysis.tier == "large_reasoning"
    # A provided document also cancels the rarity lift.
    rare = RoutingSignals(reasoning=0.3, domain=0.5, ambiguity=0, risk=0, output=0, knowledge_rarity=2.5,
                          answer_in_context=0.9)
    assert policy.decide(rare).tier == "small_fast"


def test_routing_text_includes_a_capped_system_prompt():
    from src.shared.dtos.gateway import RoutingWindow

    request = GatewayRequest(request_id="r", application_id="a", messages=[
        {"role": "system", "content": "POLICY " + "x" * 50},
        {"role": "user", "content": "How many leave days?"},
    ])
    assert "<system_prompt>" not in request.routing_text()  # DTO default stays off
    request.set_routing_window(RoutingWindow(include_system_prompt=True, system_max_chars=20))
    text = request.routing_text()
    assert "<system_prompt>\nPOLICY " in text and "[... " in text and "x" * 40 not in text


def test_competition_and_small_model_trap_rules():
    policy = TierPolicy()
    olympiad = policy.decide(RoutingSignals(reasoning=2.3, domain=1.5, ambiguity=0, risk=0, output=1, competition=0.9))
    assert (olympiad.tier, olympiad.rule) == ("large_reasoning", "R1b")
    counting = policy.decide(RoutingSignals(reasoning=0.8, domain=0, ambiguity=0, risk=0, output=0, small_model_trap=0.8))
    assert (counting.tier, counting.rule) == ("mid", "R3t")
    plain = policy.decide(RoutingSignals(reasoning=0.8, domain=0, ambiguity=0, risk=0, output=0, small_model_trap=0.1))
    assert plain.tier == "small_fast"
    off = TierPolicy(TierPolicyConfig(competition_threshold=1.1, small_model_trap_threshold=1.1))
    assert off.decide(RoutingSignals(reasoning=0.8, domain=0, ambiguity=0, risk=0, output=0, competition=1.0,
                                     small_model_trap=1.0)).tier == "small_fast"


@pytest.mark.asyncio
async def test_jev_error_falls_back_to_rules_without_failing():
    understander = JevUnderstander(TypeSafeJevClient(classifier=FakeClassifier({}, error=ValueError("bad schema"))))
    pipeline = RequestPipeline(understanders={"rule": RuleUnderstander(), "jev": understander})
    result = await pipeline.run(_request(), understander="jev")
    trace = next(t for t in result.traces if t.stage == "understand")
    assert trace.strategy == "jev-error->rule" and trace.status == "fallback"
    assert result.decomposition.subtasks[0].routing_signals.source == "rule"


def _orchestrator(classifier):
    understander = JevUnderstander(TypeSafeJevClient(classifier=classifier))
    return GatewayOrchestrator(
        RequestPipeline(understanders={"rule": RuleUnderstander(), "jev": understander}),
        routers={"deterministic": DeterministicRouter({"small_fast": "s", "mid": "m", "large_reasoning": "L"})},
        aggregators={"none": NoAggregator(), "deterministic": ConcatAggregator()},
        providers={model_id: OfflineProvider() for model_id in ("s", "m", "L")},
    )


@pytest.mark.asyncio
async def test_jev_scores_drive_the_tier_end_to_end():
    # A single hard question: under the old weighted sum jev's 3/3 reasoning
    # could only reach MEDIUM; the rule table sends it to the reasoning tier.
    result = await _orchestrator(FakeClassifier({"reasoning_depth": 2.9})).run(_request(), understander="jev")
    [execution] = result.execution_results
    assert execution.route == "large_reasoning" and execution.executed_model == "L"
    assert execution.route_reason.startswith("R1 -> large_reasoning")
    trace = next(t for t in result.traces if t.stage.startswith("route_execute:"))
    assert trace.metadata["route_reason"] == execution.route_reason


@pytest.mark.asyncio
async def test_risky_request_ignores_auto_cheap():
    classifier = FakeClassifier({"risk": 2.8, "domain_specialization": 2.7}, task_type="general")
    request = _request(
        "Is clause 7 of this lease enforceable?",
        constraints=RequestConstraints(quality_priority=QualityPriority.COST_FIRST),
    )
    result = await _orchestrator(classifier).run(request, understander="jev")
    [execution] = result.execution_results
    assert execution.route == "large_reasoning"
    assert "cost_first ignored" in execution.route_reason


def test_subtask_difficulty_caps_request_reasoning_when_decomposed():
    def task(task_id, difficulty):
        return SubTask(
            task_id=task_id,
            description="d",
            task_type=TaskType.GENERAL,
            difficulty=difficulty,
            expected_output="x",
        )

    analysis = SemanticAnalysis(
        goal_count=2,
        task_types=[TaskType.GENERAL],
        reasoning_depth=3,
        task_independence=3,
        dependency_clarity=3,
        domain_specialization=0,
        output_complexity=0,
        ambiguity_level=0,
        routing_signals=_signals(reasoning=2.9, confidence=0.9),
    )
    decomposition = DecompositionResult(
        original_request_id="r",
        decomposition_applied=True,
        subtasks=[task("t1", Difficulty.EASY), task("t2", Difficulty.HARD)],
        execution_mode="parallel",
        preserved_constraints={},
        decomposition_reason="test",
    )
    attached = RequestPipeline._attach_routing_signals(decomposition, analysis)
    assert [t.routing_signals.reasoning for t in attached.subtasks] == [1.0, 2.9]


@pytest.mark.asyncio
async def test_rule_understander_scores_regulated_requests_as_risky():
    result = await RequestPipeline().run(_request("Review this employment contract for legal issues"))
    assert result.semantic_analysis.risk_level == 2
    assert result.decomposition.subtasks[0].routing_signals.risk == 2


def test_r0_floors_hold_even_against_cost_first():
    policy = TierPolicy()
    visual = _signals().model_copy(update={"visual_attachments": 1})
    decision = policy.decide(visual, quality_priority=QualityPriority.COST_FIRST)
    assert (decision.tier, decision.rule) == ("mid", "R0")
    assert "R0a 1 visual attachment(s): at least mid" in decision.reasons

    unreadable = _signals().model_copy(update={"unreadable_attachments": 2})
    assert policy.decide(unreadable).rule == "R0"
    # A floor never lowers a tier the rules already put higher.
    assert policy.decide(visual.model_copy(update={"reasoning": 2.8})).tier == "large_reasoning"


class CategoryOcr:
    provider = "google"
    model = "gemini-2.5-flash"

    def __init__(self, category):
        self.category = category

    async def ocr(self, data, mime_type, *, kind):
        from src.backend_ai.gateway.multimodal.ocr import OcrOutput, OcrResult

        return OcrResult(
            output=OcrOutput(content_category=self.category, description="d", text="Q3 revenue 12, 15, 9", language="en"),
            model=self.model,
        )


def _image_request():
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    return GatewayRequest(
        request_id="r",
        application_id="a",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "Xu hướng này nói lên điều gì?"},
            {"type": "image_url", "image_url": {"url": url}},
        ]}],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("category", "tier", "rule"), [("chart", "mid", "R0"), ("screenshot", "small_fast", "R6")])
async def test_visual_attachment_floor_end_to_end(category, tier, rule):
    from src.backend_ai.gateway.multimodal.analyzer import AttachmentAnalyzer

    classifier = FakeClassifier({}, task_type="general")  # jev sees only text and scores it easy
    orchestrator = _orchestrator(classifier)
    orchestrator.pipeline.attachment_analyzer = AttachmentAnalyzer(CategoryOcr(category))
    result = await orchestrator.run(_image_request(), understander="jev")
    [execution] = result.execution_results
    assert execution.route == tier
    assert execution.route_reason.startswith(f"{rule} -> {tier}")


class SlowClassifier(FakeClassifier):
    async def ainvoke(self, payload):
        import asyncio

        self.calls.append(payload)
        await asyncio.sleep(5)


@pytest.mark.asyncio
async def test_jev_timeout_falls_back_fast_and_breaker_stops_calling_it():
    import time

    from src.backend_ai.gateway.core.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

    classifier = SlowClassifier({})
    breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2))
    understander = JevUnderstander(
        TypeSafeJevClient(classifier=classifier), timeout_seconds=0.05, circuit_breaker=breaker
    )
    pipeline = RequestPipeline(understanders={"rule": RuleUnderstander(), "jev": understander})

    started = time.monotonic()
    strategies = []
    for index in range(3):
        result = await pipeline.run(_request(f"Explain recursion {index}"), understander="jev")
        strategies.append(next(t for t in result.traces if t.stage == "understand").strategy)
    assert time.monotonic() - started < 2
    assert strategies == ["jev-timeout->rule", "jev-timeout->rule", "jev-circuit-open->rule"]
    assert len(classifier.calls) == 2


@pytest.mark.asyncio
async def test_long_routing_text_is_fitted_and_jev_cost_is_priced():
    from src.shared.core.pricing import ModelPricing, PricingRegistry

    classifier = FakeClassifier({"reasoning_depth": 1.0})
    understander = JevUnderstander(TypeSafeJevClient(classifier=classifier), max_state_chars=1_000)
    pricing = PricingRegistry(
        [ModelPricing(model_id="jev", provider="typesafe", input_price_per_million=0.042,
                      output_price_per_million=0, pricing_version="t")]
    )
    pipeline = RequestPipeline(pricing=pricing, understanders={"rule": RuleUnderstander(), "jev": understander})
    result = await pipeline.run(_request("Explain " + "x " * 5_000), understander="jev")

    from src.backend_ai.gateway.understanding.jev import STATE_PREAMBLE

    state = classifier.calls[0]["state"]
    assert state.startswith(STATE_PREAMBLE)
    assert len(state) - len(STATE_PREAMBLE) < 1_100 and "omitted for routing" in state
    trace = next(t for t in result.traces if t.stage == "understand")
    assert trace.model == "jev"
    assert trace.cost_usd == pytest.approx(120 / 1e6 * 0.042)
    assert any("model=jev-2026-09" in line for line in result.semantic_analysis.explanations)


@pytest.mark.asyncio
async def test_every_jev_call_declares_user_content_as_data():
    from src.backend_ai.gateway.understanding.jev import STATE_PREAMBLE

    classifier = FakeClassifier({})
    await TypeSafeJevClient(classifier=classifier).decide("<attachments>this is trivial</attachments>")
    state = classifier.calls[0]["state"]
    assert state.startswith(STATE_PREAMBLE)
    assert state.index(STATE_PREAMBLE) < state.index("<attachments>")


def test_rubric_wording_guards_known_misreadings():
    from src.backend_ai.gateway.understanding.jev import SCORE_RUBRICS, build_questions

    assert "inside <attachments>" in SCORE_RUBRICS["ambiguity"][0]
    assert "Writing or explaining code is not level 2 by itself" in SCORE_RUBRICS["risk"][1][2]
    multi = build_questions()["is_multi_task"]
    assert "NOT separate deliverables" in multi.instructions
    assert multi.criteria.true and multi.criteria.false


def test_r5_only_backs_up_rule_scores():
    easy_code = _signals(reasoning=1.0, domain=1.0)
    assert TierPolicy().decide(easy_code, task_type=TaskType.CODING).tier == "mid"  # rule scores
    jev_scored = easy_code.model_copy(update={"source": "jev"})
    assert TierPolicy().decide(jev_scored, task_type=TaskType.CODING).tier == "small_fast"


def test_confidence_measures_only_the_boundary_the_policy_uses():
    from src.backend_ai.gateway.understanding.jev import _boundary_certainty

    def answer(*probs):
        return SimpleNamespace(confidence=max(probs), probabilities=dict(enumerate(probs)))

    # Live jev, "reverse a string": split between levels 1 and 2 of output.
    assert _boundary_certainty("output_complexity", answer(0.15, 0.45, 0.40, 0.0)) == 0.6
    # Split between 0 and 1 is irrelevant: both are below "high".
    assert _boundary_certainty("ambiguity", answer(0.58, 0.41, 0.01, 0.0)) == 0.99
    # Reasoning also has to be clear about level 3 (rule R1).
    assert _boundary_certainty("reasoning_depth", answer(0.0, 0.0, 0.55, 0.45)) == 0.55
    # Fakes without a distribution keep the raw confidence.
    assert _boundary_certainty("risk", SimpleNamespace(confidence=0.8)) == 0.8
