from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from src.backend_ai.gateway.providers.base import (
    GenerationOptions,
    ProviderClient,
    ProviderResponse,
    ProviderStreamEvent,
)
from src.backend_ai.latency.context import LatencyRequestContext, current_latency_context
from src.backend_ai.latency.contracts import LatencyObservation, ModelKey
from src.backend_ai.latency.features import rebind_features
from src.backend_ai.latency.telemetry import LatencyCollector


class LatencyObservedProvider(ProviderClient):
    """Observe one provider call at a time without changing its response contract."""

    def __init__(
        self,
        inner: ProviderClient,
        *,
        bindings: Mapping[str, ModelKey],
        collector: LatencyCollector,
    ) -> None:
        self.inner = inner
        self.bindings = dict(bindings)
        self.collector = collector
        self.name = inner.name

    def _capture(
        self,
        context: LatencyRequestContext | None,
        task_id: str,
        model_id: str,
    ) -> tuple[ModelKey, Any] | None:
        if context is None or context.mode in {"off", "disabled"}:
            return None
        key = self.bindings.get(model_id) or context.bindings.get(model_id)
        if key is None:
            return None
        features = context.features_for(task_id, key.digest())
        if features is None:
            features = context.features_for(task_id)
        if features is None:
            return None
        return key, features if features.model_key.digest() == key.digest() else rebind_features(features, key)

    def _record(
        self,
        *,
        context: LatencyRequestContext,
        key: ModelKey,
        features,
        attempt_id: str,
        task_id: str,
        model_id: str,
        started_at: float,
        dispatched_at: datetime,
        status: str,
        error_category: str | None = None,
        ttft_ms: float | None = None,
        consumer_pause_ms: float = 0.0,
        actual_payload_chars: int | None = None,
    ) -> None:
        total_ms = self.collector.finish_attempt(key.digest(), started_at)
        resolution = context.resolutions_by_model.get(key.catalog_model_id)
        observation = LatencyObservation(
            attempt_id=attempt_id,
            request_id=context.request_id,
            task_id=task_id,
            application_id=context.application_id,
            features=features,
            source=context.source,
            mode=context.mode,
            dispatched_at=dispatched_at,
            status=status,  # type: ignore[arg-type]
            finished_at=self.collector.utcnow(),
            provider_total_ms=total_ms,
            catalog_ms=(resolution.evidence.get("catalog_ms") if resolution else None),
            predicted_ms=(resolution.predicted_ms if resolution else None),
            ttft_ms=ttft_ms,
            consumer_pause_ms=consumer_pause_ms,
            prediction_version=context.artifact.version if context.artifact else None,
            error_category=error_category,
            training_eligible=(consumer_pause_ms <= self.collector.config.stream_pause_exclusion_ms),
            actual_payload_chars=actual_payload_chars,
            resolution_source=resolution.source if resolution is not None else None,
            resolution_reason=resolution.gate_reason if resolution is not None else None,
        )
        self.collector.enqueue(observation)

    async def generate(
        self,
        prompt: str,
        *,
        task_id: str,
        model_id: str,
        timeout_seconds: float,
        options: GenerationOptions | None = None,
    ) -> ProviderResponse:
        context = current_latency_context()
        capture = self._capture(context, task_id, model_id)
        if capture is None:
            return await self.inner.generate(
                prompt, task_id=task_id, model_id=model_id, timeout_seconds=timeout_seconds, options=options
            )
        key, features = capture
        started_at = self.collector.begin_attempt(key.digest())
        dispatched_at = self.collector.utcnow()
        attempt_id = str(uuid4())
        try:
            response = await self.inner.generate(
                prompt, task_id=task_id, model_id=model_id, timeout_seconds=timeout_seconds, options=options
            )
        except asyncio.CancelledError:
            self._record(
                context=context, key=key, features=features, attempt_id=attempt_id, started_at=started_at,
                task_id=task_id, model_id=model_id,
                dispatched_at=dispatched_at, status="cancelled", error_category="cancelled",
                actual_payload_chars=len(prompt),
            )
            raise
        except TimeoutError:
            self._record(
                context=context, key=key, features=features, attempt_id=attempt_id, started_at=started_at,
                task_id=task_id, model_id=model_id,
                dispatched_at=dispatched_at, status="timeout", error_category="timeout",
                actual_payload_chars=len(prompt),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - provider exceptions must pass through unchanged
            self._record(
                context=context, key=key, features=features, attempt_id=attempt_id, started_at=started_at,
                task_id=task_id, model_id=model_id,
                dispatched_at=dispatched_at, status="error", error_category=type(exc).__name__[:80],
                actual_payload_chars=len(prompt),
            )
            raise
        self._record(
            context=context, key=key, features=features, attempt_id=attempt_id, started_at=started_at,
            task_id=task_id, model_id=model_id,
            dispatched_at=dispatched_at, status="success", actual_payload_chars=len(prompt),
        )
        return response

    async def stream(
        self,
        prompt: str,
        *,
        task_id: str,
        model_id: str,
        timeout_seconds: float,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        context = current_latency_context()
        capture = self._capture(context, task_id, model_id)
        if capture is None:
            async for event in self.inner.stream(
                prompt, task_id=task_id, model_id=model_id, timeout_seconds=timeout_seconds, options=options
            ):
                yield event
            return

        key, features = capture
        started_at = self.collector.begin_attempt(key.digest())
        dispatched_at = self.collector.utcnow()
        attempt_id = str(uuid4())
        iterator = self.inner.stream(
            prompt, task_id=task_id, model_id=model_id, timeout_seconds=timeout_seconds, options=options
        ).__aiter__()
        status = "incomplete"
        error_category = None
        first_output_ms = None
        consumer_pause_ms = 0.0
        final_seen = False
        try:
            while True:
                try:
                    event = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                if first_output_ms is None and ((event.text and event.text.strip()) or event.tool_delta):
                    first_output_ms = (self.collector.clock() - started_at) * 1000
                if event.final is not None:
                    final_seen = True
                before_yield = self.collector.clock()
                yield event
                consumer_pause_ms += max(0.0, (self.collector.clock() - before_yield) * 1000)
            status = "success" if final_seen else "incomplete"
            if status == "incomplete":
                error_category = "missing_final_receipt"
        except asyncio.CancelledError:
            status = "cancelled"
            error_category = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - keep the adapter's exception unchanged
            status = "error"
            error_category = type(exc).__name__[:80]
            raise
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                with contextlib.suppress(BaseException):
                    await close()
            self._record(
                context=context, key=key, features=features, attempt_id=attempt_id, started_at=started_at,
                task_id=task_id, model_id=model_id,
                dispatched_at=dispatched_at, status=status, error_category=error_category,
                ttft_ms=first_output_ms, consumer_pause_ms=consumer_pause_ms,
                actual_payload_chars=len(prompt),
            )
