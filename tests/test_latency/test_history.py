from datetime import UTC, datetime, timedelta

import pytest

from src.backend_ai.latency.contracts import LatencyConfig, LatencyFeatures, LatencyObservation, ModelKey
from src.backend_ai.latency.history import LatencyHistoryCache, build_history

BASE = datetime(2026, 10, 14, tzinfo=UTC)
KEY = ModelKey("model", "provider", "upstream", "fingerprint", "openai")


def _observation(
    attempt: str,
    dispatch_seconds: float,
    finish_seconds: float,
    *,
    status: str = "success",
    latency_ms: float = 100.0,
    source: str = "playground",
    execution_mode: str = "live",
    training_eligible: bool = True,
    predicted_ms: float | None = None,
    catalog_ms: float | None = None,
    version: str | None = None,
) -> LatencyObservation:
    dispatched = BASE + timedelta(seconds=dispatch_seconds)
    finished = BASE + timedelta(seconds=finish_seconds)
    features = LatencyFeatures(KEY, "profile", dispatched, {"input_proxy": 100, "output_budget": 512})
    return LatencyObservation(
        attempt, f"request-{attempt}", "task", "app", features, source, "observe", dispatched,
        status, finished, latency_ms, catalog_ms=catalog_ms, predicted_ms=predicted_ms,
        prediction_version=version, execution_mode=execution_mode,
        training_eligible=training_eligible,
    )


def test_history_uses_only_completed_past_outcomes_and_keeps_timeout_diagnostic():
    rows = [
        _observation("a", 1, 2, latency_ms=100),
        _observation("b", 3, 4, latency_ms=200),
        _observation("c", 5, 6, latency_ms=300),
        _observation("d", 7, 8, latency_ms=400),
        _observation("timeout", 9, 10, status="timeout", latency_ms=3000),
        _observation("future", 9.5, 11, latency_ms=900),
    ]

    snapshot = build_history(rows, as_of=BASE + timedelta(seconds=10), model_version="v2", config=LatencyConfig())
    stats = snapshot.profiles["profile"]

    assert stats.recent_success_count == 4
    assert stats.recent_count == 5
    assert stats.timeout_rate_30m == 0.2
    assert stats.inter_arrival_ms == 2000
    assert stats.newest_finished_at == BASE + timedelta(seconds=10)
    assert stats.recent_pairs == ()


def test_history_filters_internal_offline_and_stream_pause_samples():
    rows = [
        _observation("valid", 1, 2, latency_ms=5000),
        _observation("internal", 2, 3, source="internal"),
        _observation("offline", 3, 4, execution_mode="offline"),
        _observation("paused", 4, 5, training_eligible=False),
    ]

    stats = build_history(rows, as_of=BASE + timedelta(seconds=5), model_version=None, config=LatencyConfig()).profiles[
        "profile"
    ]

    assert stats.recent_success_count == 1
    assert stats.median_ms == 5000


@pytest.mark.asyncio
async def test_cache_replace_does_not_double_count_and_failed_refresh_keeps_old_snapshot(monkeypatch):
    rows = [_observation("a", 1, 2), _observation("b", 3, 4)]
    offloads = []

    async def inline_thread(function, *args, **kwargs):
        offloads.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", inline_thread)

    class Repository:
        async def read_window(self, *_args, **_kwargs):
            return rows

    cache = LatencyHistoryCache(LatencyConfig(), initial_at=BASE)
    await cache.refresh(Repository(), BASE + timedelta(seconds=5))
    first = cache.snapshot()
    await cache.refresh(Repository(), BASE + timedelta(seconds=5))
    second = cache.snapshot()
    assert second.profiles["profile"].recent_success_count == 2
    assert offloads == ["build_history", "build_history"]

    class BrokenRepository:
        async def read_window(self, *_args, **_kwargs):
            raise RuntimeError("db outage")

    with pytest.raises(RuntimeError):
        await cache.refresh(BrokenRepository(), BASE + timedelta(seconds=10))
    assert cache.snapshot() is second
    assert first.built_at == second.built_at == BASE + timedelta(seconds=5)
