from __future__ import annotations

import asyncio
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from src.backend_ai.latency.contracts import HistorySnapshot, LatencyConfig, LatencyObservation, ProfileStats

_CENSORED = frozenset({"timeout", "cancelled", "incomplete"})
_EXCLUDED_SOURCE_PARTS = ("internal", "probe", "comparison", "offline", "training", "trainer")


def is_live_target_observation(observation: LatencyObservation) -> bool:
    if observation.execution_mode.lower() != "live":
        return False
    source = observation.source.lower()
    return not any(part in source for part in _EXCLUDED_SOURCE_PARTS)


def is_usable_latency_label(observation: LatencyObservation) -> bool:
    return (
        is_live_target_observation(observation)
        and observation.training_eligible
        and observation.status == "success"
        and observation.provider_total_ms is not None
        and math.isfinite(observation.provider_total_ms)
        and observation.provider_total_ms > 0
        and observation.finished_at is not None
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _ewma(rows: list[LatencyObservation], half_life_seconds: float) -> float | None:
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: (row.finished_at, row.attempt_id))
    current = float(ordered[0].provider_total_ms)
    previous_at = ordered[0].finished_at
    for row in ordered[1:]:
        finished_at = row.finished_at
        elapsed_seconds = max(0.0, (finished_at - previous_at).total_seconds())
        alpha = 1.0 - math.exp(-math.log(2) * elapsed_seconds / half_life_seconds)
        current = alpha * float(row.provider_total_ms) + (1.0 - alpha) * current
        previous_at = finished_at
    return current


def build_history(
    rows: Sequence[LatencyObservation],
    *,
    as_of: datetime,
    model_version: str | None,
    config: LatencyConfig,
) -> HistorySnapshot:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    lower = as_of - timedelta(days=config.training_window_days)
    groups: dict[str, list[LatencyObservation]] = defaultdict(list)
    for row in rows:
        if row.dispatched_at < lower or row.dispatched_at > as_of:
            continue
        if row.finished_at is None or row.finished_at > as_of:
            continue
        if not is_live_target_observation(row):
            continue
        groups[row.features.profile_key].append(row)

    profiles: dict[str, ProfileStats] = {}
    recent_cutoff = as_of - timedelta(minutes=30)
    for profile_key, attempts in groups.items():
        attempts.sort(key=lambda row: (row.dispatched_at, row.attempt_id))
        successes = [row for row in attempts if is_usable_latency_label(row)]
        recent_attempts = [row for row in attempts if row.dispatched_at >= recent_cutoff]
        recent_successes = [row for row in successes if row.dispatched_at >= recent_cutoff]
        success_values = [float(row.provider_total_ms) for row in recent_successes]
        all_recent_count = len(recent_attempts)
        timeout_count = sum(row.status == "timeout" for row in recent_attempts)
        error_count = sum(row.status == "error" for row in recent_attempts)
        censored_count = sum(row.status in _CENSORED for row in recent_attempts)
        dispatches = [row.dispatched_at for row in attempts]
        inter_arrival = (
            max(0.0, (dispatches[-1] - dispatches[-2]).total_seconds() * 1000) if len(dispatches) >= 2 else None
        )
        newest = max((row.finished_at for row in attempts if row.finished_at is not None), default=None)
        measurement_age = max(0.0, (as_of - newest).total_seconds() * 1000) if newest else None
        recent_pairs = []
        residuals = []
        for row in successes:
            if row.prediction_version != model_version or row.predicted_ms is None:
                continue
            pair = {
                "attempt_id": row.attempt_id,
                "finished_at": row.finished_at.isoformat(),
                "predicted_ms": row.predicted_ms,
                "actual_ms": row.provider_total_ms,
                "catalog_ms": row.catalog_ms,
                "ewma_ms": row.features.values.get("ewma_ms"),
            }
            recent_pairs.append(pair)
            if row.predicted_ms > 0 and row.provider_total_ms and row.provider_total_ms > 0:
                residuals.append(
                    {
                        "attempt_id": row.attempt_id,
                        "finished_at": row.finished_at.isoformat(),
                        "residual_log": math.log1p(row.provider_total_ms) - math.log1p(row.predicted_ms),
                    }
                )
        recent_pairs = recent_pairs[-50:]
        residuals = residuals[-200:]
        input_values = [
            float(row.features.values["input_proxy"])
            for row in successes
            if isinstance(row.features.values.get("input_proxy"), (int, float))
        ]
        budget_values = [
            float(row.features.values["output_budget"])
            for row in successes
            if isinstance(row.features.values.get("output_budget"), (int, float))
        ]
        profiles[profile_key] = ProfileStats(
            train_count=len(successes),
            recent_success_count=len(successes),
            recent_count=all_recent_count,
            newest_finished_at=newest,
            timeout_rate_30m=timeout_count / all_recent_count if all_recent_count else None,
            censored_rate_30m=censored_count / all_recent_count if all_recent_count else None,
            error_rate_30m=error_count / all_recent_count if all_recent_count else None,
            inter_arrival_ms=inter_arrival,
            measurement_age_ms=measurement_age,
            ewma_ms=_ewma(recent_successes, config.residual_half_life_seconds),
            median_ms=statistics.median(success_values) if success_values else None,
            p90_ms=_percentile(success_values, 0.9),
            std_ms=statistics.pstdev(success_values) if len(success_values) > 1 else (0.0 if success_values else None),
            success_count_30m=len(recent_successes),
            input_proxy_min=min(input_values) if input_values else None,
            input_proxy_max=max(input_values) if input_values else None,
            output_budget_min=min(budget_values) if budget_values else None,
            output_budget_max=max(budget_values) if budget_values else None,
            recent_pairs=tuple(recent_pairs),
            residuals=tuple(residuals),
        )
    return HistorySnapshot(as_of, profiles)


class LatencyHistoryCache:
    """Atomically swaps immutable, bounded snapshots away from request routes."""

    def __init__(
        self,
        config: LatencyConfig,
        *,
        model_version: str | None = None,
        initial_at: datetime | None = None,
    ) -> None:
        self.config = config
        self.model_version = model_version
        self._snapshot = HistorySnapshot(initial_at or datetime.now(UTC), {})

    def snapshot(self) -> HistorySnapshot:
        return self._snapshot

    async def refresh(self, repository, now: datetime) -> None:
        rows = await repository.read_window(
            now,
            days=self.config.training_window_days,
            limit=self.config.max_training_rows,
        )
        candidate = await asyncio.to_thread(
            build_history,
            rows,
            as_of=now,
            model_version=self.model_version,
            config=self.config,
        )
        self._snapshot = candidate

    def pin_model_version(self, version: str | None) -> None:
        self.model_version = version
