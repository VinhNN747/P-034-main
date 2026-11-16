from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.dtos.applications import ApplicationCreate
from src.backend.services.application_service import ApplicationService
from src.shared.database.base import Base
from src.shared.dtos.gateway import (
    AggregationResult,
    AggregationStatus,
    CostSummary,
    DecompositionResult,
    Difficulty,
    ExecutionResultTrace,
    GatewayRequest,
    PipelineResult,
    StageTrace,
    SubTask,
    SubtaskContribution,
    TaskType,
    ValidationResult,
)
from src.shared.services.gateway_persistence_service import GatewayPersistenceService


@pytest_asyncio.fixture
async def persistence_context(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        application = await ApplicationService(session).create(
            "owner-1", ApplicationCreate(slug="runtime-demo", name="Runtime demo")
        )
        yield session, application.id
    await engine.dispose()


def _pipeline_result(
    application_id: str,
    request_id: str,
    *,
    execution_status: str = "success",
    aggregation_status: AggregationStatus = AggregationStatus.COMPLETE,
) -> PipelineResult:
    task = SubTask(
        task_id="task-1",
        description="answer the request",
        task_type=TaskType.GENERAL,
        difficulty=Difficulty.EASY,
        expected_output="answer",
        input_from={},
    )
    result = "offline:model-1:task-1" if execution_status == "success" else None
    return PipelineResult(
        request=GatewayRequest(request_id=request_id, application_id=application_id, prompt="hello"),
        validation=ValidationResult(accepted=True),
        decomposition=DecompositionResult(
            original_request_id=request_id,
            decomposition_applied=False,
            subtasks=[task],
            execution_mode="single",
            decomposition_reason="test",
        ),
        execution_results=[
            ExecutionResultTrace(
                task_id="task-1",
                status=execution_status,
                result=result,
                selected_model="model-1",
                executed_model="model-1" if result else None,
                fallback=False,
                route="model-1",
                input_tokens=4,
                output_tokens=2,
                cost_usd=0.25,
                latency_ms=12.5,
                failure_type="PermanentProviderError" if not result else None,
                failure_reason="bad request" if not result else None,
            )
        ],
        aggregation=AggregationResult(
            original_request_id=request_id,
            status=aggregation_status,
            final_answer=result if aggregation_status == AggregationStatus.COMPLETE else None,
            contributions=[
                SubtaskContribution(
                    task_id="task-1",
                    status=execution_status,
                    used_in_final_answer=bool(result),
                    excerpt=result,
                )
            ],
            missing_goal_ids=[] if result else ["goal-1"],
            strategy="none",
            reason="test",
        ),
        traces=[
            StageTrace(
                request_id=request_id,
                stage="route_execute:task-1",
                strategy="offline",
                model="model-1",
                status="success" if result else "error",
                input_tokens=4,
                output_tokens=2,
                cost_usd=0.25,
                latency_ms=12.5,
                error=None if result else "bad request",
                metadata={"cost_estimated": True},
            )
        ],
        costs=CostSummary(total_cost=0.25, routing_cost=0.25, estimated=True),
    )


@pytest.mark.asyncio
async def test_persistence_saves_successful_partial_and_failed_runs(persistence_context):
    session, application_id = persistence_context
    service = GatewayPersistenceService(session)

    await service.save_run(_pipeline_result(application_id, "req-success"))
    await service.save_run(
        _pipeline_result(
            application_id,
            "req-partial",
            execution_status="success",
            aggregation_status=AggregationStatus.PARTIAL,
        )
    )
    await service.save_run(
        _pipeline_result(
            application_id,
            "req-failed",
            execution_status="failed",
            aggregation_status=AggregationStatus.FAILED,
        )
    )

    detail = await service.get_request("req-success")
    assert detail is not None
    assert detail.status == "completed"
    assert detail.total_cost == 0.25
    assert len(await service.list_tasks("req-success")) == 1
    assert len(await service.list_route_decisions("req-success")) == 1
    assert len(await service.list_executions("req-success")) == 1
    assert len(await service.list_traces("req-success")) == 1
    assert (await service.get_aggregation("req-success")).status == "complete"

    assert (await service.get_request("req-partial")).status == "partial"
    assert (await service.get_request("req-failed")).status == "failed"


@pytest.mark.asyncio
async def test_persistence_lookup_returns_not_found_for_unknown_request(persistence_context):
    session, _ = persistence_context
    assert await GatewayPersistenceService(session).get_request("missing") is None
