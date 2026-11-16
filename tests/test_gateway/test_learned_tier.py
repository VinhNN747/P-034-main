import pytest

from src.backend_ai.gateway.factory import build_shadow_tier_policy
from src.backend_ai.gateway.routing.learned_tier import (
    FEATURES,
    LearnedTierModel,
    LearnedTierPolicy,
)
from src.backend_ai.gateway.routing.tier_policy import TIER_ORDER, TierPolicy
from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService
from src.shared.config import Settings
from src.shared.dtos.gateway import RequestConstraints, RoutingSignals, TaskType
from tests.test_gateway.test_jev_db_routing_and_decomposition import EXTERNAL_BLOCKED, _task
from tests.test_gateway.test_routing_service import routing_context  # noqa: F401 - fixture


def _model(prefer: str) -> LearnedTierModel:
    """A fixed model that always prefers one tier (intercepts only)."""
    return LearnedTierModel(
        version="test-model",
        coef=[[0.0] * len(FEATURES) for _ in TIER_ORDER],
        intercept=[5.0 if tier == prefer else 0.0 for tier in TIER_ORDER],
        classes=list(TIER_ORDER),
    )


def _signals(**scores) -> RoutingSignals:
    base = {"reasoning": 0, "domain": 0, "ambiguity": 0, "risk": 0, "output": 0}
    return RoutingSignals(**(base | scores))


def test_shipped_weights_load_and_give_a_distribution():
    model = LearnedTierModel.load()
    assert model is not None and model.classes == list(TIER_ORDER)
    probabilities = model.probabilities(_signals(reasoning=2.9, domain=2.5), TaskType.REASONING)
    assert set(probabilities) == set(TIER_ORDER)
    assert sum(probabilities.values()) == pytest.approx(1.0)
    easy = model.probabilities(_signals(), TaskType.GENERAL)
    assert max(easy, key=easy.get) == "small_fast"


def test_mismatched_weights_file_is_rejected(tmp_path):
    path = tmp_path / "weights.json"
    path.write_text('{"version": "x", "features": ["reasoning"], "classes": [], "coef": [], "intercept": []}')
    with pytest.raises(ValueError):
        LearnedTierModel.load(path)
    assert LearnedTierModel.load(tmp_path / "missing.json") is None


def test_learned_policy_keeps_the_r2b_guard_and_floors():
    policy = LearnedTierPolicy(_model("small_fast"))
    plain = policy.decide(_signals(), task_type=TaskType.GENERAL)
    assert (plain.rule, plain.tier) == ("ML", "small_fast")

    risky = policy.decide(_signals(risk=2.9), task_type=TaskType.GENERAL)
    assert (risky.rule, risky.tier) == ("ML+R2b", "large_reasoning")

    # Live-data questions are not "risky" in the R2b sense.
    live = policy.decide(_signals(risk=2.9, live_data=0.9), task_type=TaskType.GENERAL)
    assert live.tier == "small_fast"

    # Hard floors from TierPolicy still apply on top of the model.
    chart = policy.decide(_signals(visual_attachments=1), task_type=TaskType.GENERAL)
    assert (chart.tier, chart.rule) == ("mid", "R0")


def test_shadow_policy_follows_the_settings_flag():
    assert isinstance(build_shadow_tier_policy(Settings(gateway_tier_shadow_model_enabled=True)), LearnedTierPolicy)
    assert build_shadow_tier_policy(Settings(gateway_tier_shadow_model_enabled=False)) is None


@pytest.mark.asyncio
async def test_shadow_decision_is_logged_but_never_routes(routing_context):  # noqa: F811
    session, app = routing_context["session"], routing_context["application_id"]
    plain = await DatabaseRoutingService(session, app).route(_task(), EXTERNAL_BLOCKED)
    assert plain.shadow is None

    shadowed = await DatabaseRoutingService(
        session, app, shadow_policy=LearnedTierPolicy(_model("large_reasoning"))
    ).route(_task(), EXTERNAL_BLOCKED)
    assert shadowed.selected_model == plain.selected_model == routing_context["cheap"]
    assert shadowed.shadow["policy"] == "test-model"
    assert shadowed.shadow["tier"] == "large_reasoning"
    assert shadowed.shadow["actual_tier"] == "small_fast"
    assert shadowed.shadow["agrees"] is False


class _BrokenPolicy(TierPolicy):
    def decide(self, *args, **kwargs):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_shadow_failure_does_not_break_routing(routing_context):  # noqa: F811
    service = DatabaseRoutingService(
        routing_context["session"], routing_context["application_id"], shadow_policy=_BrokenPolicy()
    )
    decision = await service.route(_task(), RequestConstraints(blocked_providers={"external"}))
    assert decision.selected_model == routing_context["cheap"]
    assert decision.shadow == {"error": "RuntimeError: boom"}
