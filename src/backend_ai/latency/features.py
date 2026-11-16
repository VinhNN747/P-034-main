from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime

from src.backend_ai.gateway.providers.base import GenerationOptions
from src.backend_ai.latency.contracts import HistorySnapshot, LatencyFeatures, ModelKey
from src.shared.dtos.gateway import RoutingSignals


def _profile_key(
    key: ModelKey,
    *,
    protocol: str,
    streaming: bool,
    has_tools: bool,
    has_schema: bool,
    n: int,
) -> str:
    profile = {
        "model": key.digest(),
        "protocol": protocol,
        "streaming": streaming,
        "has_tools": has_tools,
        "has_schema": has_schema,
        "n": n,
    }
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_features(
    key: ModelKey,
    *,
    material_chars: int,
    options: GenerationOptions,
    task_type: str | None,
    signals: RoutingSignals | None,
    now: datetime,
    history: HistorySnapshot,
    local_load: Mapping[str, float],
    protocol: str = "native",
    streaming: bool = False,
    routing_objective: str | None = None,
) -> LatencyFeatures:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    budget_values = [value for value in (options.max_output_tokens, options.hard_completion_token_limit) if value is not None]
    if material_chars < 0 or options.n < 1 or any(value < 0 for value in budget_values):
        raise ValueError("material length, n and output limit must be non-negative")
    output_budget = min(budget_values) if budget_values else None

    has_tools = bool(options.tools)
    has_schema = bool(options.json_schema or options.response_format)
    profile = _profile_key(
        key, protocol=protocol, streaming=streaming, has_tools=has_tools,
        has_schema=has_schema, n=options.n,
    )
    stats = history.profiles.get(profile)
    hour = now.hour + now.minute / 60 + now.second / 3600
    values: dict[str, float | int | str | bool | None] = {
        "model_key_hash": key.digest(),
        "provider_kind": key.provider_kind,
        "protocol": protocol,
        "task_type": task_type or "unknown",
        # Reporting metadata only: the objective changes candidate selection,
        # not the provider's latency target, so the trainer deliberately ignores it.
        "routing_objective": getattr(routing_objective, "value", routing_objective) or "unknown",
        "streaming": int(streaming),
        "has_tools": int(has_tools),
        "has_schema": int(has_schema),
        "n": options.n,
        "input_proxy": math.ceil(material_chars / 3),
        "output_budget": output_budget,
        "output_budget_missing": int(output_budget is None),
        "reasoning": None if signals is None else float(signals.reasoning),
        "reasoning_missing": int(signals is None),
        "domain": None if signals is None else float(signals.domain),
        "domain_missing": int(signals is None),
        "ambiguity": None if signals is None else float(signals.ambiguity),
        "ambiguity_missing": int(signals is None),
        "risk": None if signals is None else float(signals.risk),
        "risk_missing": int(signals is None),
        "output": None if signals is None else float(signals.output),
        "output_missing": int(signals is None),
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "weekday_sin": math.sin(2 * math.pi * now.weekday() / 7),
        "weekday_cos": math.cos(2 * math.pi * now.weekday() / 7),
        "inter_arrival_ms": None if stats is None else stats.inter_arrival_ms,
        "inter_arrival_missing": int(stats is None or stats.inter_arrival_ms is None),
        "measurement_age_ms": None if stats is None else stats.measurement_age_ms,
        "history_missing": int(stats is None or stats.recent_success_count == 0),
        "ewma_ms": None if stats is None else stats.ewma_ms,
        "median_ms": None if stats is None else stats.median_ms,
        "p90_ms": None if stats is None else stats.p90_ms,
        "std_ms": None if stats is None else stats.std_ms,
        "error_rate_30m": None if stats is None else stats.error_rate_30m,
        "timeout_rate_30m": None if stats is None else stats.timeout_rate_30m,
        "success_count_30m": None if stats is None else stats.success_count_30m,
        "local_inflight": max(0.0, float(local_load.get("inflight", 0.0))),
        "local_rpm": max(0.0, float(local_load.get("rpm", 0.0))),
    }
    return LatencyFeatures(key, profile, now, values)


def rebind_features(features: LatencyFeatures, key: ModelKey) -> LatencyFeatures:
    """Keep a request's workload snapshot while binding it to an actual fallback model."""
    values = dict(features.values)
    values["model_key_hash"] = key.digest()
    values["provider_kind"] = key.provider_kind
    profile = _profile_key(
        key,
        protocol=str(values.get("protocol", "native")),
        streaming=bool(values.get("streaming", False)),
        has_tools=bool(values.get("has_tools", False)),
        has_schema=bool(values.get("has_schema", False)),
        n=int(values.get("n", 1)),
    )
    return LatencyFeatures(key, profile, features.observed_at, values, features.schema_version)


def build_candidate_features(
    context,
    task_id: str,
    *,
    material_chars: int,
    options: GenerationOptions,
    task_type: str | None,
    signals: RoutingSignals | None,
    now: datetime,
    protocol: str,
    streaming: bool,
    routing_objective: str | None = None,
) -> dict[str, LatencyFeatures]:
    """Build and pin candidate-specific pre-dispatch features for one route decision."""
    by_digest: dict[str, LatencyFeatures] = {}
    result: dict[str, LatencyFeatures] = {}
    for model_id, key in context.bindings.items():
        if model_id != key.catalog_model_id:
            continue
        features = by_digest.get(key.digest())
        if features is None:
            collector = context.collector
            local_load = collector.local_load(key.digest()) if collector is not None else {"inflight": 0.0, "rpm": 0.0}
            features = build_features(
                key,
                material_chars=material_chars,
                options=options,
                task_type=task_type,
                signals=signals,
                now=now,
                history=context.history,
                local_load=local_load,
                protocol=protocol,
                streaming=streaming,
                routing_objective=routing_objective,
            )
            by_digest[key.digest()] = features
            context.bind_features(task_id, features)
        result[model_id] = features
    return result
