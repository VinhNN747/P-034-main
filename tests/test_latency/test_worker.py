from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.backend_ai.latency.contracts import LatencyConfig
from src.backend_ai.workers.latency_worker import should_train

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def test_training_requires_interval_total_and_new_samples():
    config = LatencyConfig(
        mode="auto",
        training_interval_seconds=3600,
        min_training_samples=500,
        min_new_samples=200,
    )

    assert not should_train(NOW, NOW - timedelta(seconds=3599), 500, 200, 0, 0.0, config)
    assert not should_train(NOW, NOW - timedelta(hours=1), 499, 200, 0, 0.0, config)
    assert not should_train(NOW, NOW - timedelta(hours=1), 500, 199, 0, 0.0, config)
    assert should_train(NOW, NOW - timedelta(hours=1), 500, 200, 1, 0.49, config)


def test_training_defers_for_api_load_or_full_observation_queue():
    config = LatencyConfig(mode="auto", training_max_api_inflight=1)
    last_run = NOW - timedelta(hours=2)

    assert not should_train(NOW, last_run, 500, 200, 2, 0.0, config)
    assert not should_train(NOW, last_run, 500, 200, 0, 0.5, config)


def test_auto_mode_trains_but_off_mode_and_disabled_training_do_not():
    auto = LatencyConfig(mode="auto")
    off = LatencyConfig(mode="off")
    disabled = LatencyConfig(mode="auto", training_enabled=False)
    last_run = NOW - timedelta(hours=2)

    assert should_train(NOW, last_run, 500, 200, 0, 0.0, auto)
    assert not should_train(NOW, last_run, 500, 200, 0, 0.0, off)
    assert not should_train(NOW, last_run, 500, 200, 0, 0.0, disabled)


@pytest.mark.asyncio
async def test_writer_shutdown_is_bounded_and_accounts_for_dropped_queue(monkeypatch):
    from src.backend_ai.workers import latency_worker

    entered = asyncio.Event()
    never = asyncio.Event()
    stop = asyncio.Event()
    runtime = latency_worker.LatencyRuntime(LatencyConfig(mode="auto"), session_factory=None)
    runtime.collector.queue.put_nowait(object())

    async def skip_cleanup(_runtime):
        return None

    async def blocked_writer(*_args):
        entered.set()
        await never.wait()

    async def follow_stop(_runtime, stop_event):
        await stop_event.wait()

    monkeypatch.setattr(latency_worker, "_cleanup_orphan_run_dirs", skip_cleanup)
    monkeypatch.setattr(latency_worker, "run_observation_writer", blocked_writer)
    monkeypatch.setattr(latency_worker, "_refresh_loop", follow_stop)
    monkeypatch.setattr(latency_worker, "_training_loop", follow_stop)
    monkeypatch.setattr(latency_worker, "WRITER_SHUTDOWN_SECONDS", 0.01)
    monkeypatch.setattr(latency_worker, "BACKGROUND_SHUTDOWN_SECONDS", 0.1)

    task = asyncio.create_task(latency_worker.run_latency_worker(None, stop, runtime))
    await asyncio.wait_for(entered.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert runtime.collector.metrics["dropped_shutdown"] == 1
    assert runtime.collector.queue.empty()


@pytest.mark.asyncio
async def test_only_worker_that_claims_fenced_lease_starts_training(monkeypatch):
    from src.backend_ai.workers import latency_worker

    called = False

    async def start_training(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not start without a lease")

    async def claim_lease(*args, **kwargs):
        return None

    claimed = await latency_worker._start_training_if_owner(
        claim_lease, NOW, owner="worker-a", on_claimed=start_training
    )

    assert claimed is False
    assert called is False
