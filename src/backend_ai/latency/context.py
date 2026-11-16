from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from src.backend_ai.latency.contracts import (
    HistorySnapshot,
    LatencyArtifact,
    LatencyFeatures,
    LatencyResolution,
    ModelKey,
)


@dataclass(slots=True)
class LatencyRequestContext:
    request_id: str
    source: str
    mode: str
    artifact: LatencyArtifact | None
    history: HistorySnapshot
    collector: Any
    bindings: Mapping[str, ModelKey]
    application_id: str | None = None
    resolver: Any = None
    features_by_task: dict[str, LatencyFeatures] = field(default_factory=dict)
    features_by_task_model: dict[tuple[str, str], LatencyFeatures] = field(default_factory=dict)
    resolutions_by_model: dict[str, LatencyResolution] = field(default_factory=dict)

    def bind_features(self, task_id: str, features: LatencyFeatures) -> None:
        self.features_by_task.setdefault(task_id, features)
        self.features_by_task_model[(task_id, features.model_key.digest())] = features

    def features_for(self, task_id: str | None, model_id: str | None = None) -> LatencyFeatures | None:
        task_key = task_id or ""
        if model_id:
            key = self.bindings.get(model_id)
            digest = key.digest() if key is not None else model_id
            features = self.features_by_task_model.get((task_key, digest))
            if features is not None:
                return features
        return self.features_by_task.get(task_key)


_current_latency_context: ContextVar[LatencyRequestContext | None] = ContextVar(
    "current_latency_request_context", default=None
)


def current_latency_context() -> LatencyRequestContext | None:
    return _current_latency_context.get()


def latency_request_scope(context: LatencyRequestContext):
    """Context manager that always restores a caller's previous request scope."""
    class _Scope:
        def __enter__(self) -> LatencyRequestContext:
            self.token: Token[LatencyRequestContext | None] = _current_latency_context.set(context)
            return context

        def __exit__(self, exc_type, exc, traceback) -> None:
            _current_latency_context.reset(self.token)

    return _Scope()
