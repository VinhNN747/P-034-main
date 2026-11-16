import pytest

from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.understanding.base import BaseUnderstander, UnderstandingOutcome
from src.backend_ai.gateway.understanding.rules import RuleUnderstander
from src.shared.dtos.gateway import GatewayRequest, SemanticAnalysis, TaskType


class RecordingLLMUnderstander(BaseUnderstander):
    name = "llm"

    def __init__(self) -> None:
        self.called = False

    async def understand(self, request, constraints):
        self.called = True
        return UnderstandingOutcome(
            analysis=SemanticAnalysis(
                goal_count=1,
                task_types=[TaskType.REASONING],
                reasoning_depth=1,
                task_independence=0,
                dependency_clarity=3,
                domain_specialization=0,
                output_complexity=0,
                ambiguity_level=0,
                required_capabilities=["basic arithmetic"],
                explicit_constraints={},
            ),
            strategy=self.name,
        )


@pytest.mark.asyncio
async def test_explicit_rule_understander_is_not_promoted_to_llm_for_ambiguous_prompt():
    llm = RecordingLLMUnderstander()
    pipeline = RequestPipeline(understanders={"rule": RuleUnderstander(), "llm": llm})
    request = GatewayRequest(
        request_id="ambiguous-math",
        application_id="app-1",
        prompt="1 + 1 = ?",
    )

    result = await pipeline.run(request, understander="rule", decomposer="none")

    assert llm.called is False
    assert result.semantic_analysis is not None
    assert result.semantic_analysis.required_capabilities == []
