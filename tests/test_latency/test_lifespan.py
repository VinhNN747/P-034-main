from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from src.shared.config import Settings


@pytest.mark.asyncio
async def test_latency_worker_starts_when_other_workers_are_disabled(monkeypatch):
    from src.backend_ai.workers import lifespan

    settings = Settings(
        _env_file=None,
        comparison_worker_enabled=False,
        gateway_file_maintenance_enabled=False,
        gateway_latency_mode="auto",
    )
    started = asyncio.Event()

    async def fake_worker(settings, stop_event, runtime):
        started.set()
        await stop_event.wait()

    monkeypatch.setattr(lifespan, "get_settings", lambda: settings)
    monkeypatch.setattr(lifespan, "run_latency_worker", fake_worker)
    app = FastAPI()

    async with lifespan.comparison_lifespan(app):
        await asyncio.wait_for(started.wait(), timeout=1)
        assert app.state.latency_runtime.config.mode == "auto"

    assert not hasattr(app.state, "latency_runtime")
