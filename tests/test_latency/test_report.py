from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.backend_ai.latency.contracts import LatencyFeatures, LatencyObservation, ModelKey
from src.scripts.latency_learning_report import build_report
from src.scripts.latency_model_rollback import rollback_model
from src.shared.database.base import Base
from src.shared.models.latency import LatencyModelState

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def _observation(attempt_id: str, *, objective="balanced", catalog=100.0, predicted=90.0, actual=80.0):
    key = ModelKey("model", "provider", "upstream", "fingerprint", "openai")
    features = LatencyFeatures(
        key,
        "profile",
        NOW,
        {"routing_objective": objective, "ewma_ms": 95.0},
    )
    return LatencyObservation(
        attempt_id=attempt_id,
        request_id="request",
        task_id="task",
        application_id="application",
        features=features,
        source="openai_chat_completions",
        mode="shadow",
        dispatched_at=NOW - timedelta(seconds=2),
        status="success",
        finished_at=NOW - timedelta(seconds=1),
        provider_total_ms=actual,
        catalog_ms=catalog,
        predicted_ms=predicted,
        resolution_source="catalog",
        resolution_reason="shadow",
    )


def test_report_groups_by_model_profile_and_objective_without_request_content():
    report = build_report(
        [_observation("one"), _observation("two"), _observation("unknown", objective="cost_first", catalog=None, predicted=None)],
        [],
        since=NOW - timedelta(days=1),
        generated_at=NOW,
    )
    encoded = json.dumps(report, sort_keys=True)

    assert report["observation_count"] == 3
    assert len(report["groups"]) == 2
    balanced = next(group for group in report["groups"] if group["objective"] == "balanced")
    assert balanced["counts"]["success"] == 2
    assert balanced["errors_ms"]["candidate_mae"] == 10.0
    assert balanced["errors_ms"]["catalog_mae"] == 20.0
    unknown = next(group for group in report["groups"] if group["objective"] == "cost_first")
    assert unknown["errors_ms"]["candidate_mae"] is None
    assert unknown["errors_ms"]["catalog_mae"] is None
    assert "raw prompt" not in encoded and "sk-secret" not in encoded
    assert report["training_runs"] == []


@pytest.mark.asyncio
async def test_rollback_compare_and_swap_conflict_keeps_pointer(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'report-rollback.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(LatencyModelState(
            scope="generation-v1",
            active_generation=2,
            active_version="current",
            active_artifact_json={"version": "current"},
            previous_versions_json=[{"version": "previous", "artifact": {"version": "previous"}}],
        ))
        session.commit()

    class AsyncSessionAdapter:
        def __init__(self, session): self.session = session
        async def scalar(self, statement): return self.session.scalar(statement)
        async def execute(self, statement): return self.session.execute(statement)
        async def commit(self): self.session.commit()

    @asynccontextmanager
    async def factory():
        with Session(engine) as session:
            yield AsyncSessionAdapter(session)

    try:
        assert not await rollback_model(factory, expected_generation=1, version="previous")
        with Session(engine) as session:
            state = session.get(LatencyModelState, "generation-v1")
            assert state.active_generation == 2
            assert state.active_version == "current"
    finally:
        engine.dispose()

