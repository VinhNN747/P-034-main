import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from src.backend_ai.latency.contracts import LatencyConfig, LatencyFeatures, LatencyObservation, ModelKey
from src.backend_ai.latency.telemetry import LatencyCollector, run_observation_writer


def _observation(attempt_id: str) -> LatencyObservation:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    key = ModelKey("model", "provider", "upstream", "fingerprint", "openai")
    return LatencyObservation(
        attempt_id, "request", "task", "application", LatencyFeatures(key, "profile", now, {}),
        "openai_chat", "observe", now, "success", now, 10.0,
    )


def test_queue_full_drops_metrics_without_raising():
    collector = LatencyCollector(LatencyConfig(observation_queue_size=1))

    assert collector.enqueue(_observation("first")) is True
    assert collector.enqueue(_observation("second")) is False
    assert collector.metrics["dropped_queue_full"] == 1


def test_oversized_event_is_dropped_without_prompt_material():
    collector = LatencyCollector(LatencyConfig(max_event_bytes=128))
    observation = _observation("large")
    huge_metadata = {f"field-{index}": "x" * 20 for index in range(100)}
    expanded = replace(
        observation,
        features=LatencyFeatures(observation.features.model_key, "p", observation.dispatched_at, huge_metadata),
    )

    assert collector.enqueue(expanded) is False
    assert collector.metrics["dropped_oversized"] == 1


@pytest.mark.asyncio
async def test_writer_retries_in_own_session_and_drains_before_shutdown():
    collector = LatencyCollector(LatencyConfig(writer_batch_size=2, writer_flush_seconds=0.01))
    collector.enqueue(_observation("one"))
    collector.enqueue(_observation("two"))
    stored: list[str] = []
    attempts = 0

    class Repository:
        async def insert_observations(self, rows):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("db unavailable")
            stored.extend(row.attempt_id for row in rows)
            return len(rows)

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return None

    stop = asyncio.Event()
    stop.set()

    def session_factory():
        return SessionContext()

    await run_observation_writer(
        collector, session_factory, stop, repository_factory=lambda _session: Repository(), retry_delay_seconds=0
    )

    assert sorted(stored) == ["one", "two"]
    assert attempts == 2
    assert collector.metrics["writer_retries"] == 1
