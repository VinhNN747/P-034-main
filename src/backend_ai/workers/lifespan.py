"""Run the durable comparison queue inside the AI application's event loop."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from src.backend_ai.latency.contracts import LatencyConfig
from src.backend_ai.services.comparison_service import run_worker
from src.backend_ai.workers.file_maintenance import run_file_maintenance
from src.backend_ai.workers.latency_worker import LatencyRuntime, run_latency_worker
from src.shared.config import get_settings
from src.shared.database.session import create_session_factory

logger = logging.getLogger(__name__)


@asynccontextmanager
async def comparison_lifespan(app: FastAPI):
    settings = get_settings()
    latency_enabled = settings.gateway_latency_mode != "off"
    if not settings.comparison_worker_enabled and not settings.gateway_file_maintenance_enabled and not latency_enabled:
        yield
        return
    stop = asyncio.Event()
    tasks = []
    latency_runtime = None
    if settings.comparison_worker_enabled:
        task = asyncio.create_task(
            run_worker(settings.comparison_worker_poll_seconds, stop_event=stop, settings=settings),
            name="comparison-worker",
        )
        app.state.comparison_worker = task
        tasks.append(task)
    if settings.gateway_file_maintenance_enabled:
        tasks.append(asyncio.create_task(run_file_maintenance(settings, stop), name="file-maintenance"))
    if latency_enabled:
        latency_runtime = LatencyRuntime(LatencyConfig.from_settings(settings), create_session_factory())
        app.state.latency_runtime = latency_runtime
        tasks.append(asyncio.create_task(
            run_latency_worker(settings, stop, latency_runtime), name="adaptive-latency-worker"
        ))
    worker_group = asyncio.gather(*tasks)
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(worker_group), timeout=settings.comparison_worker_shutdown_seconds)
        except TimeoutError:
            logger.info("Stopping comparison worker after shutdown grace period")
            worker_group.cancel()
        finally:
            if not worker_group.done():
                worker_group.cancel()
            with suppress(asyncio.CancelledError):
                await worker_group
            if latency_runtime is not None:
                with suppress(AttributeError):
                    del app.state.latency_runtime
