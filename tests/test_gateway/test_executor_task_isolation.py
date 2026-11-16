import pytest

from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.agents.routing.executor import SubtaskExecutor
from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse
from src.shared.dtos.gateway import Difficulty, SubTask, TaskType


class PromptRecordingRouter(BaseRouter):
    name = "prompt-recording"

    async def aroute(self, subtask, upstream_results=None, **kwargs):
        return RouteDecision(
            task_id=subtask.task_id,
            tier="primary",
            strategy=self.name,
            selected_model="primary",
        )


class PromptRecordingProvider(ProviderClient):
    name = "prompt-recording"

    def __init__(self):
        self.prompts: dict[str, str] = {}

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds):
        self.prompts[task_id] = prompt
        return ProviderResponse(text=f"result:{task_id}", provider=self.name, model_id=model_id)


def _task(task_id: str, description: str) -> SubTask:
    return SubTask(
        task_id=task_id,
        description=description,
        task_type=TaskType.GENERAL,
        difficulty=Difficulty.EASY,
        expected_output=f"answer for {task_id}",
    )


@pytest.mark.asyncio
async def test_executor_keeps_independent_subtask_prompts_isolated():
    provider = PromptRecordingProvider()
    executor = SubtaskExecutor(
        PromptRecordingRouter(),
        providers={"primary": provider},
        timeout_seconds=2,
    )

    results = await executor.aexecute_subtasks(
        [
            _task("task-1", "Calculate 27 times 14."),
            _task("task-2", "Translate the gateway status into English."),
        ],
        original_prompt=(
            "Calculate 27 times 14 and translate the gateway status into English; return both tasks as one JSON object."
        ),
    )

    task_one_prompt = provider.prompts["task-1"]
    task_two_prompt = provider.prompts["task-2"]
    assert "Complete only the assigned task" in task_one_prompt
    assert "Complete only the assigned task" in task_two_prompt
    assert results[0].result != results[1].result
    assert "return both tasks as one JSON object" not in task_one_prompt
    assert "return both tasks as one JSON object" not in task_two_prompt
    assert "Translate the gateway status" not in task_one_prompt
    assert "Calculate 27 times 14" not in task_two_prompt
