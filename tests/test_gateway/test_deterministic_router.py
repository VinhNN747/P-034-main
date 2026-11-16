import pytest

from src.backend_ai.agents.routing import deterministic_router as deterministic_router
from src.backend_ai.agents.routing.deterministic_router import DeterministicRouter
from src.backend_ai.agents.routing.router import ExecutionConfig, ExecutionResult, execution_trace_rows
from src.shared.config import get_settings
from src.shared.dtos.gateway import Difficulty, SubTask, TaskType


def _task(task_type: TaskType, difficulty: Difficulty) -> SubTask:
    return SubTask(
        task_id="task-1",
        description="do the task",
        task_type=task_type,
        difficulty=difficulty,
        expected_output="text",
    )


def test_select_tier_maps_declared_difficulty():
    assert deterministic_router.select_tier(_task(TaskType.GENERAL, Difficulty.EASY)) == "small_fast"
    assert deterministic_router.select_tier(_task(TaskType.GENERAL, Difficulty.MEDIUM)) == "mid"
    assert deterministic_router.select_tier(_task(TaskType.GENERAL, Difficulty.HARD)) == "large_reasoning"


def test_select_tier_bumps_demanding_tasks_and_caps_at_large_reasoning():
    assert deterministic_router.select_tier(_task(TaskType.CODING, Difficulty.EASY)) == "mid"
    assert deterministic_router.select_tier(_task(TaskType.RESEARCH, Difficulty.MEDIUM)) == "large_reasoning"
    assert deterministic_router.select_tier(_task(TaskType.REASONING, Difficulty.HARD)) == "large_reasoning"


@pytest.mark.asyncio
async def test_deterministic_router_defers_model_resolution_to_runtime():
    decision = await DeterministicRouter().aroute(_task(TaskType.GENERAL, Difficulty.MEDIUM))

    assert decision.tier == "mid"
    assert decision.selected_model is None
    assert decision.strategy == "deterministic"


def test_execution_config_reads_demo_routing_settings(monkeypatch):
    monkeypatch.setenv("GATEWAY_ROUTING_FALLBACK_MODEL", "fallback-model")
    monkeypatch.setenv("GATEWAY_ROUTING_TIMEOUT_SECONDS", "7.5")
    get_settings.cache_clear()

    config = ExecutionConfig.from_settings()

    assert config.fallback_model == "fallback-model"
    assert config.timeout_seconds == 7.5
    get_settings.cache_clear()


def test_execution_trace_rows_use_demo_strategy_labels():
    task = _task(TaskType.GENERAL, Difficulty.EASY)
    normal = ExecutionResult(
        task_id=task.task_id,
        status="success",
        result="ok",
        selected_model="openai:gpt-4o-mini",
        executed_model="openai:gpt-4o-mini",
        fallback=False,
    )
    fallback = ExecutionResult(
        task_id=task.task_id,
        status="success",
        result="fallback",
        selected_model=None,
        executed_model="openai:gpt-4o-mini",
        fallback=True,
    )

    rows = execution_trace_rows([task, task], [normal, fallback])

    assert rows[0]["strategy/model"] == "jev-model-router / openai:gpt-4o-mini"
    assert rows[1]["strategy/model"] == "fixed-fallback / openai:gpt-4o-mini"
