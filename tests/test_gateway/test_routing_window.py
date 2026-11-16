import pytest

from src.backend_ai.agents.routing.deterministic_router import DeterministicRouter
from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.aggregation.no_aggregator import NoAggregator
from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse
from src.backend_ai.gateway.understanding.rules import RuleUnderstander
from src.shared.dtos.gateway import EffectiveConstraints, GatewayRequest, RequestConstraints, RoutingWindow

SYSTEM = "Bạn là trợ lý pháp lý của công ty X. Luôn trích dẫn nghị định liên quan. " * 20
LONG_ANSWER = "Bản tóm tắt hợp đồng: điều khoản thanh toán, phạt vi phạm, kiến trúc trách nhiệm... " * 40


def _chat(*extra, countback=1) -> GatewayRequest:
    request = GatewayRequest(
        request_id="r",
        application_id="a",
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Tóm tắt hợp đồng này và phân tích rủi ro"},
            {"role": "assistant", "content": LONG_ANSWER},
            *extra,
            {"role": "user", "content": "dịch sang tiếng Anh giúp tôi"},
        ],
    )
    request.set_routing_window(RoutingWindow(message_countback=countback))
    return request


def test_views_split_focus_context_and_instructions():
    request = _chat()
    assert request.focus_text() == "dịch sang tiếng Anh giúp tôi"
    assert request.system_text().startswith("Bạn là trợ lý pháp lý")
    context = request.context_text()
    assert context.startswith("user: Tóm tắt hợp đồng")
    assert "assistant: Bản tóm tắt" in context and "[...]" in context
    assert len(context) < 1700

    routing = request.routing_text()
    assert routing.startswith("dịch sang tiếng Anh giúp tôi")
    assert "<conversation_context" in routing
    assert "trợ lý pháp lý" not in routing  # system prompt excluded by default

    conversation = request.conversation_text()
    assert conversation.startswith("System instructions:")
    assert LONG_ANSWER.strip() in conversation

    assert "nghị định" in request.constraint_text()
    assert "Tóm tắt hợp đồng" not in request.constraint_text()


def test_countback_widens_focus_and_system_prompt_can_be_included():
    request = _chat({"role": "user", "content": "ngắn hơn"}, countback=2)
    assert request.focus_text() == "ngắn hơn\ndịch sang tiếng Anh giúp tôi"
    request.set_routing_window(RoutingWindow(include_system_prompt=True))
    assert "<system_prompt>" in request.routing_text()


def test_plain_prompt_requests_are_unchanged():
    request = GatewayRequest(request_id="r", application_id="a", prompt="Explain recursion")
    assert request.focus_text() == request.routing_text() == "Explain recursion"
    assert request.context_text() == request.conversation_text() == ""


@pytest.mark.asyncio
async def test_short_follow_up_is_not_scored_as_hard_because_of_history():
    effective = EffectiveConstraints(constraints=RequestConstraints(), resolved={})
    outcome = await RuleUnderstander().understand(_chat(), effective)
    assert outcome.analysis.reasoning_depth == 0
    assert outcome.analysis.domain_specialization == 0


@pytest.mark.asyncio
async def test_pipeline_plans_only_the_latest_ask():
    result = await RequestPipeline(config=GatewayConfig()).run(_chat())
    assert result.goal_extraction.decision != "multi"
    [task] = result.decomposition.subtasks
    assert task.description == "dịch sang tiếng Anh giúp tôi"


class CapturingProvider(ProviderClient):
    name = "offline"

    def __init__(self):
        self.prompts: list[str] = []

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds):
        self.prompts.append(prompt)
        return ProviderResponse(text="ok", provider=self.name, model_id=model_id)


@pytest.mark.asyncio
async def test_executor_still_sees_the_conversation():
    provider = CapturingProvider()
    orchestrator = GatewayOrchestrator(
        RequestPipeline(),
        routers={"deterministic": DeterministicRouter()},
        aggregators={"none": NoAggregator(), "deterministic": ConcatAggregator()},
        providers={"offline": provider},
    )
    await orchestrator.run(_chat())
    [prompt] = provider.prompts
    assert "Conversation so far" in prompt
    assert "Bản tóm tắt hợp đồng" in prompt
    assert prompt.index("Conversation so far") < prompt.index("Task task-1: dịch sang tiếng Anh")


def test_hard_constraint_sets_serialize_in_a_stable_order():
    from src.shared.dtos.gateway import (
        ConstraintEnforcement,
        ConstraintSource,
        EffectiveConstraints,
        ResolvedConstraint,
    )

    def effective(values):
        return EffectiveConstraints(
            constraints=RequestConstraints(),
            resolved={
                "allowed_models": ResolvedConstraint(
                    value=values,
                    source=ConstraintSource.APPLICATION_POLICY,
                    enforcement=ConstraintEnforcement.HARD,
                    confidence=1.0,
                    contributing_sources=[ConstraintSource.APPLICATION_POLICY],
                )
            },
        )

    ids = [f"model-{index}" for index in range(12)]
    assert effective(set(ids)).hard_values() == effective(set(reversed(ids))).hard_values()
    assert effective(set(ids)).hard_values()["allowed_models"] == sorted(ids)


def test_validator_treats_set_like_constraints_as_unordered():
    from src.backend_ai.gateway.decomposition.validator import _same_constraint_value

    assert _same_constraint_value(["b", "a"], ["a", "b"])
    assert not _same_constraint_value(["a"], ["a", "b"])
    assert _same_constraint_value("json", "json")
