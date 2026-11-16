import pytest

from src.backend_ai.gateway.aggregation.llm_aggregator import LLMAggregator
from src.backend_ai.gateway.llm.client import StructuredModelClient
from src.shared.dtos.gateway import (
    AggregationStatus,
    DecompositionResult,
    Difficulty,
    EffectiveConstraints,
    GatewayRequest,
    ModelUsage,
    RequestConstraints,
    StructuredModelResponse,
    SubTask,
    TaskType,
)


class RecordingClient(StructuredModelClient):
    def __init__(self):
        self.prompt = ""

    async def generate(self, prompt, schema, model, timeout):
        self.prompt = prompt
        return StructuredModelResponse(
            data={"final_answer": '{"answer": "ok"}'},
            usage=ModelUsage(),
            model=model,
        )


def _constraints() -> EffectiveConstraints:
    return EffectiveConstraints(
        constraints=RequestConstraints(output_format="json"),
        resolved={},
    )


@pytest.mark.asyncio
async def test_llm_aggregator_explicitly_requests_raw_json_without_markdown():
    client = RecordingClient()
    aggregator = LLMAggregator(client)
    task = SubTask(
        task_id="task-1",
        description="answer the question",
        task_type=TaskType.GENERAL,
        difficulty=Difficulty.EASY,
        expected_output="answer",
    )
    decomposition = DecompositionResult(
        original_request_id="req-json-prompt",
        decomposition_applied=True,
        subtasks=[task],
        execution_mode="parallel",
        decomposition_reason="test",
    )

    outcome = await aggregator.aggregate(
        GatewayRequest(request_id="req-json-prompt", application_id="app", prompt="answer"),
        decomposition,
        [type("Execution", (), {"task_id": "task-1", "status": "success", "result": "ok"})()],
        _constraints(),
    )

    assert outcome.result.status == AggregationStatus.COMPLETE
    assert "valid JSON" in client.prompt
    assert "Markdown" in client.prompt


@pytest.mark.asyncio
async def test_public_llm_aggregation_uses_raw_final_producer_and_hard_cap():
    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.shared.dtos.gateway import OutputEvidence
    class Client(RecordingClient):
        def __init__(self):
            super().__init__()
            self.options = None
        async def generate_output(self, prompt, model, timeout, *, options):
            self.options = options
            return ProviderResponse(text='brief', provider='offline', model_id=model,
                input_tokens=6, output_tokens=4, usage_received=True, output_evidence=OutputEvidence(finish_reason='length'))
        async def generate(self, *args):
            raise AssertionError('Public output must not be hidden inside a JSON wrapper')
    client = Client()
    task = SubTask(task_id='task-1', description='answer', task_type=TaskType.GENERAL,
        difficulty=Difficulty.EASY, expected_output='answer')
    decomposition = DecompositionResult(original_request_id='public-aggregate', decomposition_applied=False,
        subtasks=[task], execution_mode='single', decomposition_reason='test')
    outcome = await LLMAggregator(client).aggregate(
        GatewayRequest(request_id='public-aggregate', application_id='app', prompt='answer',
            metadata={'client_metadata': {'api_surface': 'openai_chat_completions'}, 'openai_hard_completion_token_limit': 48}),
        decomposition, [type('Execution', (), {'task_id': 'task-1', 'status': 'success', 'result': 'long internal answer'})()],
        EffectiveConstraints(constraints=RequestConstraints(max_output_tokens=24), resolved={}))
    assert client.options.hard_completion_token_limit == client.options.max_output_tokens == 24
    assert outcome.result.final_answer == 'brief' and outcome.output_evidence.finish_reason == 'length'
    assert outcome.usage.input_tokens == 6 and outcome.usage.output_tokens == 4
