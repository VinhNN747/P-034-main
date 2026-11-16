import pytest

from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.understanding.jev import JevUnderstander, TypeSafeJevClient
from src.backend_ai.gateway.understanding.rules import RuleUnderstander
from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService
from src.shared.dtos.gateway import (
    Difficulty,
    RequestConstraints,
    RoutingSignals,
    SubTask,
    TaskType,
)
from tests.test_gateway.test_routing_service import routing_context  # noqa: F401 - fixture
from tests.test_gateway.test_tier_policy import FakeClassifier, _request

EXTERNAL_BLOCKED = RequestConstraints(blocked_providers={"external"})


def _task(reasoning=0.0, risk=0.0, domain=0.0):
    return SubTask(
        task_id="task-1",
        description="d",
        task_type=TaskType.GENERAL,
        difficulty=Difficulty.MEDIUM,
        required_capabilities=["chat"],
        expected_output="x",
        routing_signals=RoutingSignals(reasoning=reasoning, domain=domain, ambiguity=0, risk=risk, output=0),
    )


@pytest.mark.asyncio
async def test_database_router_uses_the_policy_tier_inside_the_pool(routing_context):  # noqa: F811
    """Unlabelled pool: tiers come from price, so the policy tier decides
    between the cheap and the strong model in the team's ranking."""
    service = DatabaseRoutingService(routing_context["session"], routing_context["application_id"])

    easy = await service.route(_task(), EXTERNAL_BLOCKED)
    assert easy.selected_model == routing_context["cheap"]
    assert easy.reason.startswith("R6 -> small_fast") and "tiers by price ranking" in easy.reason

    hard = await service.route(_task(reasoning=2.8), EXTERNAL_BLOCKED)
    assert hard.selected_model == routing_context["quality"]
    assert hard.reason.startswith("R1 -> large_reasoning")

    # Without routing signals the team's difficulty-based ranking is unchanged.
    plain = await service.route(_task().model_copy(update={"routing_signals": None}), EXTERNAL_BLOCKED)
    assert plain.reason is None


@pytest.mark.asyncio
async def test_policy_floor_survives_cost_first(routing_context):  # noqa: F811
    """cost_first weights carry no difficulty_fit in the team's ranking; with
    routing signals the policy tier still counts, so high risk keeps the
    strong model even for a cost-first caller."""
    service = DatabaseRoutingService(routing_context["session"], routing_context["application_id"])
    cost_first = RequestConstraints(blocked_providers={"external"}, quality_priority="cost_first")
    risky = await service.route(_task(risk=2.8, domain=2.8), cost_first)
    assert risky.selected_model == routing_context["quality"]
    assert "cost_first ignored: risk is high" in risky.reason


@pytest.mark.asyncio
async def test_database_router_prefers_catalog_quality_tier_labels(routing_context):  # noqa: F811
    from src.shared.models.gateway import Model

    session = routing_context["session"]
    (await session.get(Model, routing_context["cheap"])).quality_tier = "economy"
    (await session.get(Model, routing_context["quality"])).quality_tier = "standard"
    await session.commit()

    decision = await DatabaseRoutingService(session, routing_context["application_id"]).route(
        _task(reasoning=2.2, domain=2.2), EXTERNAL_BLOCKED
    )
    assert decision.selected_model == routing_context["quality"]
    assert "tiers by quality_tier labels" in decision.reason


def _jev_pipeline(multi_probability):
    classifier = FakeClassifier({}, task_type="general", multi=multi_probability)
    understander = JevUnderstander(TypeSafeJevClient(classifier=classifier))
    return RequestPipeline(config=GatewayConfig(decomposition_mode="legacy"), understanders={"rule": RuleUnderstander(), "jev": understander})


AND_PROMPT = "Summarize the report and translate the title to French"


@pytest.mark.asyncio
async def test_jev_splits_an_unclear_and_request():
    result = await _jev_pipeline(0.9).run(_request(AND_PROMPT), understander="jev")

    assert [task.description for task in result.decomposition.subtasks] == [
        "Summarize the report",
        "translate the title to French",
    ]
    decision = next(t for t in result.traces if t.stage == "decomposition_decision")
    assert decision.metadata["band"] == "jev-multi"
    assert result.goal_extraction.decision == "multi"
    assert "confirmed by jev" in result.goal_extraction.reason


@pytest.mark.asyncio
async def test_jev_keeps_an_unclear_request_whole_when_it_sees_one_deliverable():
    result = await _jev_pipeline(0.1).run(_request(AND_PROMPT), understander="jev")
    assert len(result.decomposition.subtasks) == 1
    decision = next(t for t in result.traces if t.stage == "decomposition_decision")
    assert decision.metadata["band"] == "jev-single"


@pytest.mark.asyncio
async def test_clear_extractor_decisions_still_win_over_jev():
    numbered = "1. Summarize the report\n2. Translate the title to French"
    result = await _jev_pipeline(0.0).run(_request(numbered), understander="jev")
    assert len(result.decomposition.subtasks) == 2
    decision = next(t for t in result.traces if t.stage == "decomposition_decision")
    assert decision.metadata["band"] == "deterministic-multi"


@pytest.mark.asyncio
async def test_without_jev_the_unclear_request_keeps_the_score_path():
    result = await RequestPipeline(config=GatewayConfig(decomposition_mode="legacy")).run(_request(AND_PROMPT))
    decision = next(t for t in result.traces if t.stage == "decomposition_decision")
    assert decision.metadata["band"] not in {"jev-multi", "jev-single"}
    assert result.semantic_analysis.multi_task_probability is None
