import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.backend_ai.latency.contracts import LatencyArtifact, LatencyFeatures, LatencyObservation, ModelKey
from src.backend_ai.repositories.latency import LatencyRepository
from src.shared.database.base import Base
from src.shared.models.latency import LatencyModelState


class AsyncSessionAdapter:
    """Run the repository's real SQL statements through a local SQLite session."""

    def __init__(self, session: Session):
        self._session = session

    def get_bind(self):
        return self._session.get_bind()

    def add(self, instance):
        self._session.add(instance)

    async def get(self, model, ident):
        return self._session.get(model, ident)

    async def execute(self, statement):
        return self._session.execute(statement)

    async def scalar(self, statement):
        return self._session.scalar(statement)

    async def scalars(self, statement):
        return self._session.scalars(statement)

    async def commit(self):
        self._session.commit()

    async def flush(self):
        self._session.flush()

    async def rollback(self):
        self._session.rollback()


def _database(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'latency.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(LatencyModelState(scope="generation-v1"))
        session.commit()
    return engine


def _observation(attempt_id: str) -> LatencyObservation:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    key = ModelKey("model", "provider", "upstream/model", "fingerprint", "openai")
    features = LatencyFeatures(key, "profile", now, {"input_proxy": 10})
    return LatencyObservation(
        attempt_id, "request", "task", "application", features, "openai_chat", "observe", now,
        "success", now + timedelta(milliseconds=100), 100.0,
        resolution_source="catalog", resolution_reason="shadow",
    )


@pytest.mark.asyncio
async def test_duplicate_attempt_inserts_once(tmp_path):
    engine = _database(tmp_path)
    try:
        with Session(engine) as session:
            repository = LatencyRepository(AsyncSessionAdapter(session))
            assert await repository.insert_observations([_observation("attempt")]) == 1
            assert await repository.insert_observations([_observation("attempt")]) == 0
            rows = await repository.read_window(datetime(2026, 10, 15, tzinfo=UTC), days=7, limit=100)
            assert len(rows) == 1
            assert rows[0].features.values == {"input_proxy": 10}
            assert rows[0].mode == "observe"
            assert rows[0].resolution_source == "catalog"
            assert rows[0].resolution_reason == "shadow"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_expired_owner_cannot_publish(tmp_path):
    engine = _database(tmp_path)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    try:
        with Session(engine) as first, Session(engine) as second:
            first_repo = LatencyRepository(AsyncSessionAdapter(first))
            second_repo = LatencyRepository(AsyncSessionAdapter(second))
            old_lease = await first_repo.claim_training("worker-a", now, 2)
            new_lease = await second_repo.claim_training("worker-b", now + timedelta(seconds=3), 2)
            assert old_lease is not None and new_lease is not None
            artifact = LatencyArtifact("latency-v1", "v1", (), (), 0.0)

            assert await first_repo.publish(old_lease, artifact, {}, 0, now + timedelta(seconds=3)) is False
            assert await second_repo.publish(new_lease, artifact, {}, 0, now + timedelta(seconds=3)) is True
            generation, active = await second_repo.read_active()
            assert generation == 1 and active is not None and active.version == "v1"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_two_sessions_claim_one_lease(tmp_path):
    engine = _database(tmp_path)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    try:
        with Session(engine) as session_a, Session(engine) as session_b:
            repo_a = LatencyRepository(AsyncSessionAdapter(session_a))
            repo_b = LatencyRepository(AsyncSessionAdapter(session_b))
            leases = await asyncio.gather(
                repo_a.claim_training("worker-a", now, 120),
                repo_b.claim_training("worker-b", now, 120),
            )
            assert sum(lease is not None for lease in leases) == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_published_run_updates_its_running_lease_record(tmp_path):
    engine = _database(tmp_path)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    try:
        with Session(engine) as session:
            repository = LatencyRepository(AsyncSessionAdapter(session))
            lease = await repository.claim_training("worker", now, 120)
            assert lease is not None
            await repository.start_training_run(
                lease,
                expected_active_generation=0,
                cutoff=now,
                sample_count=100,
                now=now,
            )
            artifact = LatencyArtifact("latency-v1", "v1", (), (), 0.0)

            assert await repository.publish(
                lease,
                artifact,
                {"sample_count": 100, "dataset_digest": "a" * 64},
                0,
                now + timedelta(seconds=1),
            )
            progress = await repository.read_training_progress()

            assert progress is not None
            assert progress["status"] == "published"
            assert progress["sample_count"] == 100
            assert progress["dataset_cutoff"] == now + timedelta(seconds=1)
    finally:
        engine.dispose()
