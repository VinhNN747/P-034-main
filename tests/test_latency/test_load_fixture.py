from __future__ import annotations

import asyncio
import sys

import pytest

from src.backend_ai.workers.latency_worker import _supervise_command


@pytest.mark.asyncio
async def test_stub_api_ticker_progresses_while_training_child_runs():
    ticks = 0
    finished = asyncio.Event()

    async def stub_api():
        nonlocal ticks
        while not finished.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    ticker = asyncio.create_task(stub_api())
    try:
        result = await _supervise_command(
            [sys.executable, "-c", "import time; time.sleep(0.15)"],
            timeout_seconds=5,
            max_rss_mb=512,
            stop_event=asyncio.Event(),
            lease_alive=lambda: True,
            poll_seconds=0.01,
        )
    finally:
        finished.set()
        await ticker

    assert result.status == "completed"
    assert ticks >= 5
