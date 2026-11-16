from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from src.backend_ai.latency.contracts import LatencyConfig, LatencyObservation, thaw

logger = logging.getLogger(__name__)


class LatencyCollector:
    """Bounded, non-blocking observation queue and process-local load counters."""

    def __init__(
        self,
        config: LatencyConfig,
        clock: Callable[[], float] = time.monotonic,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.clock = clock
        self.utcnow = utc_clock
        self.queue: asyncio.Queue[LatencyObservation] = asyncio.Queue(maxsize=config.observation_queue_size)
        self.metrics: dict[str, int] = {
            "observed": 0,
            "persisted": 0,
            "dropped_queue_full": 0,
            "dropped_oversized": 0,
            "dropped_writer_error": 0,
            "dropped_shutdown": 0,
            "writer_retries": 0,
            "prediction_applied": 0,
            "prediction_fallback": 0,
        }
        self.resolution_reasons: dict[str, int] = {}
        self._inflight: dict[str, int] = {}
        self._dispatches: dict[str, deque[float]] = {}

    @property
    def queue_fraction(self) -> float:
        return self.queue.qsize() / self.config.observation_queue_size

    @property
    def inflight_count(self) -> int:
        return sum(self._inflight.values())

    def begin_attempt(self, model_key_hash: str) -> float:
        now = self.clock()
        self._inflight[model_key_hash] = self._inflight.get(model_key_hash, 0) + 1
        dispatches = self._dispatches.setdefault(model_key_hash, deque())
        dispatches.append(now)
        self._prune_dispatches(dispatches, now)
        return now

    def finish_attempt(self, model_key_hash: str, started_at: float) -> float:
        finished = self.clock()
        current = self._inflight.get(model_key_hash, 0)
        if current <= 1:
            self._inflight.pop(model_key_hash, None)
        else:
            self._inflight[model_key_hash] = current - 1
        return max(0.0, (finished - started_at) * 1000)

    def local_load(self, model_key_hash: str) -> Mapping[str, float]:
        now = self.clock()
        dispatches = self._dispatches.setdefault(model_key_hash, deque())
        self._prune_dispatches(dispatches, now)
        return {"inflight": float(self._inflight.get(model_key_hash, 0)), "rpm": float(len(dispatches))}

    @staticmethod
    def _prune_dispatches(dispatches: deque[float], now: float) -> None:
        while dispatches and now - dispatches[0] > 60:
            dispatches.popleft()

    def enqueue(self, observation: LatencyObservation) -> bool:
        payload = {
            "attempt_id": observation.attempt_id,
            "request_id": observation.request_id,
            "task_id": observation.task_id,
            "application_id": observation.application_id,
            "model_key": observation.features.model_key.digest(),
            "profile_key": observation.features.profile_key,
            "schema": observation.features.schema_version,
            "values": thaw(observation.features.values),
            "source": observation.source,
            "mode": observation.mode,
            "status": observation.status,
            "dispatched_at": observation.dispatched_at.isoformat(),
            "finished_at": observation.finished_at.isoformat() if observation.finished_at else None,
            "provider_total_ms": observation.provider_total_ms,
            "ttft_ms": observation.ttft_ms,
            "consumer_pause_ms": observation.consumer_pause_ms,
            "error_category": observation.error_category,
        }
        try:
            size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        except (TypeError, ValueError):
            self.metrics["dropped_oversized"] += 1
            return False
        if size > self.config.max_event_bytes:
            self.metrics["dropped_oversized"] += 1
            return False
        try:
            self.queue.put_nowait(observation)
        except asyncio.QueueFull:
            self.metrics["dropped_queue_full"] += 1
            return False
        self.metrics["observed"] += 1
        return True


async def run_observation_writer(
    collector: LatencyCollector,
    session_factory: Callable[[], Any],
    stop_event: asyncio.Event,
    *,
    repository_factory: Callable[[Any], Any] | None = None,
    retry_delay_seconds: float = 0.1,
) -> None:
    """Persist small batches through fresh sessions; every failure stays fail-open."""
    from src.backend_ai.repositories.latency import LatencyRepository

    make_repository = repository_factory or LatencyRepository
    queue = collector.queue
    while not stop_event.is_set() or not queue.empty():
        batch: list[LatencyObservation] = []
        try:
            first = await asyncio.wait_for(queue.get(), timeout=collector.config.writer_flush_seconds)
            batch.append(first)
        except TimeoutError:
            continue
        deadline = asyncio.get_running_loop().time() + collector.config.writer_flush_seconds
        while len(batch) < collector.config.writer_batch_size:
            if stop_event.is_set():
                try:
                    batch.append(queue.get_nowait())
                    continue
                except asyncio.QueueEmpty:
                    break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(queue.get(), timeout=remaining))
            except TimeoutError:
                break

        persisted = False
        for attempt in range(collector.config.writer_retries + 1):
            try:
                async with session_factory() as session:
                    inserted = await make_repository(session).insert_observations(batch)
                collector.metrics["persisted"] += inserted
                persisted = True
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - metrics persistence must never fail request work
                if attempt < collector.config.writer_retries:
                    collector.metrics["writer_retries"] += 1
                    if retry_delay_seconds:
                        await asyncio.sleep(retry_delay_seconds * (attempt + 1))
                else:
                    collector.metrics["dropped_writer_error"] += len(batch)
                    logger.warning("latency observation batch dropped after DB failure: %s", type(exc).__name__)
        for _ in batch:
            queue.task_done()
        if not persisted:
            continue
