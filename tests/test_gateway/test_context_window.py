import pytest

from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService
from src.shared.dtos.gateway import (
    AttachmentAnalysis,
    AttachmentKind,
    DecompositionResult,
    Difficulty,
    GatewayRequest,
    RequestConstraints,
    SemanticAnalysis,
    SubTask,
    TaskType,
)
from tests.test_gateway.test_jev_db_routing_and_decomposition import _task
from tests.test_gateway.test_routing_service import routing_context  # noqa: F401 - fixture


async def _catalog(session, context, windows):
    """cheap = small tier, quality = mid tier, blocked = large tier, with the given context windows."""
    from src.shared.models.gateway import Model

    for key, (label, window) in {
        "cheap": ("simple", windows[0]), "quality": ("mid", windows[1]), "blocked": ("hard", windows[2]),
    }.items():
        model = await session.get(Model, context[key])
        model.quality_tier, model.context_window = label, window
    await session.commit()


def _sized(context_chars, **scores):
    task = _task(**scores)
    return task.model_copy(update={"routing_signals": task.routing_signals.model_copy(
        update={"context_chars": context_chars})})


@pytest.mark.asyncio
async def test_a_request_too_long_for_the_small_model_moves_to_the_nearest_tier_that_fits(routing_context):  # noqa: F811
    session, app = routing_context["session"], routing_context["application_id"]
    await _catalog(session, routing_context, (16_000, 200_000, 1_000_000))

    # About 30k tokens (90k chars / 3) cannot go to the 16k-token small model.
    decision = await DatabaseRoutingService(session, app).route(_sized(90_000))
    assert decision.selected_model == routing_context["quality"]
    assert "excluded cheap-fast" in decision.reason
    assert "context escalated from small_fast" in decision.reason
    assert routing_context["cheap"] not in decision.fallback_models

    # A short request on the same pool keeps the small model and says nothing about context.
    short = await DatabaseRoutingService(session, app).route(_sized(3_000))
    assert short.selected_model == routing_context["cheap"]
    assert "context" not in short.reason


@pytest.mark.asyncio
async def test_the_output_budget_counts_against_the_window(routing_context):  # noqa: F811
    session, app = routing_context["session"], routing_context["application_id"]
    await _catalog(session, routing_context, (16_000, 200_000, 1_000_000))
    # 30k chars ~ 10k tokens fits 16k alone, but not with 8k output tokens on top.
    fits = await DatabaseRoutingService(session, app).route(_sized(30_000))
    assert fits.selected_model == routing_context["cheap"]
    too_big = await DatabaseRoutingService(session, app).route(_sized(30_000), RequestConstraints(max_output_tokens=8_000))
    assert too_big.selected_model == routing_context["quality"]


@pytest.mark.asyncio
async def test_unknown_windows_are_kept_and_nothing_fitting_keeps_the_widest(routing_context):  # noqa: F811
    session, app = routing_context["session"], routing_context["application_id"]
    # A model that declares no window is not assumed to be too small.
    await _catalog(session, routing_context, (None, 200_000, 1_000_000))
    unknown = await DatabaseRoutingService(session, app).route(_sized(900_000))
    assert unknown.selected_model == routing_context["cheap"]

    # Every window declared and none large enough: keep the widest instead of failing here.
    await _catalog(session, routing_context, (16_000, 32_000, 64_000))
    widest = await DatabaseRoutingService(session, app).route(_sized(900_000))
    assert widest.selected_model == routing_context["blocked"]
    assert "exceeds every model" in widest.reason


def test_context_chars_counts_history_latest_turn_and_attachment_text():
    request = GatewayRequest(request_id="r", application_id="a", messages=[
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "u" * 5_000},
        {"role": "assistant", "content": "a" * 5_000},
        {"role": "user", "content": "ok, thanks"},
    ])
    task = SubTask(task_id="t", description="d", task_type=TaskType.GENERAL, difficulty=Difficulty.EASY,
                   expected_output="text")
    analysis = SemanticAnalysis(goal_count=1, task_types=[TaskType.GENERAL], reasoning_depth=0, task_independence=0,
                                dependency_clarity=3, domain_specialization=0, output_complexity=0, ambiguity_level=0)
    attachment = AttachmentAnalysis(attachment_id="m1p1", message_index=1, part_index=1, kind=AttachmentKind.PDF,
                                    source="data", status="analyzed", method="pdf_text_layer",
                                    extracted_text="x" * 20_000)
    result = RequestPipeline._attach_routing_signals(
        DecompositionResult(original_request_id="r", decomposition_applied=False, subtasks=[task],
                            execution_mode="single", decomposition_reason="single goal"),
        analysis, [attachment], request,
    )
    signals = result.subtasks[0].routing_signals
    # The latest turn stays short for difficulty ...
    assert signals.input_chars == len("ok, thanks") + 20_000
    # ... but what the executor sends (system + history + latest turn + attachment text) is all counted.
    assert signals.context_chars > 100 + 5_000 + 5_000 + 20_000
