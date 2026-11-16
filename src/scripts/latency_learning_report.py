"""Print a bounded, read-only report for adaptive latency observations."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.backend_ai.repositories.latency import LatencyRepository
from src.shared.database.session import create_engine_from_settings

_OBJECTIVES = {"cost_first", "balanced", "latency_first", "quality_first"}


def _positive(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0


def _error_metrics(rows, value_for):
    pairs = [
        (float(value), float(row.provider_total_ms))
        for row in rows
        if row.status == "success"
        and _positive(row.provider_total_ms)
        and _positive(value := value_for(row))
    ]
    if not pairs:
        return {"count": 0, "mae": None, "wape": None}
    errors = [abs(prediction - actual) for prediction, actual in pairs]
    actual_total = sum(actual for _, actual in pairs)
    return {
        "count": len(pairs),
        "mae": sum(errors) / len(errors),
        "wape": sum(errors) / actual_total if actual_total > 0 else None,
    }


def _runtime_metrics(run: dict[str, Any]) -> dict[str, Any] | None:
    report = run.get("report")
    metrics = report.get("runtime_metrics") if isinstance(report, dict) else None
    if not isinstance(metrics, dict):
        return None
    allowed_scalars = {"sampled_at", "duration_seconds", "peak_rss_mb", "exit_code", "process_status", "queue_depth"}
    result = {key: metrics[key] for key in allowed_scalars if key in metrics}
    for name in ("collector", "resolution_reasons"):
        counter = metrics.get(name)
        if isinstance(counter, dict):
            result[name] = {
                str(key)[:80]: max(0, int(value))
                for key, value in counter.items()
                if isinstance(value, (int, float)) and math.isfinite(float(value))
            }
    return result


def build_report(rows, training_runs, *, since: datetime, generated_at: datetime) -> dict[str, Any]:
    if since.tzinfo is None or since.utcoffset() is None or generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("report timestamps must be timezone-aware")
    groups = defaultdict(list)
    for row in rows:
        objective = row.features.values.get("routing_objective")
        objective = objective if isinstance(objective, str) and objective in _OBJECTIVES else "unknown"
        key = (
            row.features.model_key.digest(),
            row.features.model_key.catalog_model_id,
            row.features.model_key.provider_id,
            row.features.profile_key,
            objective,
        )
        groups[key].append(row)

    summaries = []
    for (model_hash, model_id, provider_id, profile, objective), grouped_rows in sorted(groups.items()):
        statuses = Counter(row.status for row in grouped_rows)
        sources = Counter(row.resolution_source or "unknown" for row in grouped_rows)
        reasons = Counter(row.resolution_reason or "unknown" for row in grouped_rows)
        latest = max((row.finished_at for row in grouped_rows if row.finished_at is not None), default=None)
        latest_age = (generated_at - latest).total_seconds() if latest else None
        common = [
            row for row in grouped_rows
            if row.status == "success"
            and _positive(row.provider_total_ms)
            and _positive(row.predicted_ms)
            and _positive(row.catalog_ms)
            and _positive(row.features.values.get("ewma_ms"))
        ]
        summaries.append({
            "model_key_hash": model_hash,
            "catalog_model_id": model_id,
            "provider_id": provider_id,
            "profile_key": profile,
            "objective": objective,
            "counts": {
                "attempts": len(grouped_rows),
                "success": statuses["success"],
                "error": statuses["error"],
                "timeout": statuses["timeout"],
                "cancelled": statuses["cancelled"],
                "incomplete": statuses["incomplete"],
                "training_eligible": sum(bool(row.training_eligible) for row in grouped_rows),
            },
            "freshness": {
                "latest_finished_at": latest.isoformat() if latest else None,
                "latest_age_seconds": max(0.0, latest_age) if latest_age is not None else None,
            },
            "resolution": {
                "source_counts": dict(sorted(sources.items())),
                "reason_counts": dict(sorted(reasons.items())),
                "predicted_fraction": sources["predicted"] / len(grouped_rows) if grouped_rows else None,
            },
            "errors_ms": {
                "common_comparison_count": len(common),
                "candidate_mae": _error_metrics(common, lambda row: row.predicted_ms)["mae"],
                "candidate_wape": _error_metrics(common, lambda row: row.predicted_ms)["wape"],
                "catalog_mae": _error_metrics(common, lambda row: row.catalog_ms)["mae"],
                "catalog_wape": _error_metrics(common, lambda row: row.catalog_ms)["wape"],
                "ewma_mae": _error_metrics(common, lambda row: row.features.values.get("ewma_ms"))["mae"],
                "ewma_wape": _error_metrics(common, lambda row: row.features.values.get("ewma_ms"))["wape"],
            },
        })

    clean_runs = []
    for run in training_runs:
        clean_runs.append({
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "candidate_version": run.get("candidate_version"),
            "sample_count": run.get("sample_count"),
            "created_at": run["created_at"].isoformat() if run.get("created_at") else None,
            "finished_at": run["finished_at"].isoformat() if run.get("finished_at") else None,
            "runtime_metrics": _runtime_metrics(run),
        })
    latest_runtime = next((run["runtime_metrics"] for run in clean_runs if run["runtime_metrics"] is not None), None)
    return {
        "generated_at": generated_at.isoformat(),
        "since": since.isoformat(),
        "observation_count": len(rows),
        "group_count": len(summaries),
        "groups": summaries,
        "training_runs": clean_runs,
        "latest_runtime_counters": latest_runtime,
        "telemetry_limitations": [
            "Worker queue/drop counters are process-local snapshots recorded at training runs; they are null before the first recorded run.",
            "A missing baseline or prediction is reported as null and excluded from its error metric, never as zero.",
        ],
    }


def _parse_since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--since must include a UTC offset")
    return parsed.astimezone(UTC)


async def _report(since: datetime) -> dict[str, Any]:
    engine = create_engine_from_settings(None)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            repository = LatencyRepository(session)
            rows = await repository.read_report_rows(since, limit=20_000)
            runs = await repository.read_report_runs(since, limit=100)
        return build_report(rows, runs, since=since, generated_at=datetime.now(UTC))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, type=_parse_since, help="inclusive ISO timestamp, for example 2026-10-01T00:00:00Z")
    parser.add_argument("--format", choices=("json",), default="json")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_report(args.since)), ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
