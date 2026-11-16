from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.backend_ai.latency.contracts import LatencyConfig, LatencyFeatures, LatencyObservation, ModelKey, thaw
from src.backend_ai.latency.history import is_live_target_observation, is_usable_latency_label


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    schema_version: str
    cutoff: datetime
    train_rows: tuple[LatencyObservation, ...]
    validation_rows: tuple[LatencyObservation, ...]
    digest: str


def _row_dict(row: LatencyObservation) -> dict:
    key = row.features.model_key
    return {
        "attempt_id": row.attempt_id,
        "request_id": row.request_id,
        "task_id": row.task_id,
        "application_id": row.application_id,
        "model_key": {
            "catalog_model_id": key.catalog_model_id,
            "provider_id": key.provider_id,
            "upstream_model_name": key.upstream_model_name,
            "connection_fingerprint": key.connection_fingerprint,
            "provider_kind": key.provider_kind,
        },
        "profile_key": row.features.profile_key,
        "feature_schema": row.features.schema_version,
        "features": thaw(row.features.values),
        "source": row.source,
        "mode": row.mode,
        "execution_mode": row.execution_mode,
        "status": row.status,
        "dispatched_at": row.dispatched_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "provider_total_ms": row.provider_total_ms,
        "ttft_ms": row.ttft_ms,
        "consumer_pause_ms": row.consumer_pause_ms,
        "catalog_ms": row.catalog_ms,
        "predicted_ms": row.predicted_ms,
        "prediction_version": row.prediction_version,
        "error_category": row.error_category,
        "training_eligible": row.training_eligible,
        "actual_payload_chars": row.actual_payload_chars,
    }


def _digest_rows(train_rows: Sequence[LatencyObservation], validation_rows: Sequence[LatencyObservation]) -> str:
    canonical = {
        "train": [_row_dict(row) for row in train_rows],
        "validation": [_row_dict(row) for row in validation_rows],
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def build_dataset(
    rows: Sequence[LatencyObservation],
    *,
    cutoff: datetime,
    config: LatencyConfig,
) -> DatasetSnapshot:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    lower = cutoff - timedelta(days=config.training_window_days)
    eligible = [
        row for row in rows
        if row.dispatched_at >= lower
        and row.dispatched_at <= cutoff
        and row.finished_at is not None
        and row.finished_at <= cutoff
        and is_live_target_observation(row)
        and is_usable_latency_label(row)
    ]
    eligible.sort(key=lambda row: (row.dispatched_at, row.attempt_id))
    if len(eligible) > config.max_training_rows:
        eligible = eligible[-config.max_training_rows :]

    counts: dict[str, int] = {}
    for row in eligible:
        key = row.features.model_key.digest()
        counts[key] = counts.get(key, 0) + 1
    allowed_models = {
        key for key, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[: config.max_model_groups]
    }
    eligible = [row for row in eligible if row.features.model_key.digest() in allowed_models]

    request_dispatch: dict[str, datetime] = {}
    for row in eligible:
        request_dispatch[row.request_id] = min(request_dispatch.get(row.request_id, row.dispatched_at), row.dispatched_at)
    ordered_requests = sorted(request_dispatch.items(), key=lambda item: (item[1], item[0]))
    if len(ordered_requests) < 2:
        train_rows = tuple(eligible)
        validation_rows: tuple[LatencyObservation, ...] = ()
    else:
        validation_group_count = max(1, math.ceil(len(ordered_requests) * 0.2))
        training_requests = {request for request, _ in ordered_requests[:-validation_group_count]}
        validation_groups = ordered_requests[-validation_group_count:]
        validation_requests = {request for request, _ in validation_groups}
        validation_start = validation_groups[0][1]
        train_rows = tuple(
            row for row in eligible
            if row.request_id in training_requests and row.finished_at is not None and row.finished_at <= validation_start
        )
        validation_rows = tuple(row for row in eligible if row.request_id in validation_requests)
    digest = _digest_rows(train_rows, validation_rows)
    return DatasetSnapshot("latency-v1", cutoff, train_rows, validation_rows, digest)


def write_dataset(snapshot: DatasetSnapshot, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "schema_version": snapshot.schema_version,
        "cutoff": snapshot.cutoff.isoformat(),
        "train_rows": [_row_dict(row) for row in snapshot.train_rows],
        "validation_rows": [_row_dict(row) for row in snapshot.validation_rows],
        "digest": snapshot.digest,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _parse_row(payload: dict) -> LatencyObservation:
    key = ModelKey(**payload["model_key"])
    dispatched_at = _parse_datetime(payload["dispatched_at"])
    assert dispatched_at is not None
    features = LatencyFeatures(
        key, payload["profile_key"], dispatched_at, payload["features"], payload["feature_schema"]
    )
    return LatencyObservation(
        attempt_id=payload["attempt_id"], request_id=payload["request_id"], task_id=payload.get("task_id"),
        application_id=payload.get("application_id"), features=features, source=payload["source"],
        mode=payload["mode"], dispatched_at=dispatched_at, status=payload["status"],
        finished_at=_parse_datetime(payload.get("finished_at")), provider_total_ms=payload.get("provider_total_ms"),
        ttft_ms=payload.get("ttft_ms"), consumer_pause_ms=payload.get("consumer_pause_ms", 0.0),
        catalog_ms=payload.get("catalog_ms"), predicted_ms=payload.get("predicted_ms"),
        prediction_version=payload.get("prediction_version"), error_category=payload.get("error_category"),
        execution_mode=payload.get("execution_mode", "live"),
        training_eligible=payload.get("training_eligible", True),
        actual_payload_chars=payload.get("actual_payload_chars"),
    )


def read_dataset(path: Path) -> DatasetSnapshot:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "latency-v1":
        raise ValueError("unsupported dataset schema")
    snapshot = DatasetSnapshot(
        schema_version=payload["schema_version"],
        cutoff=_parse_datetime(payload["cutoff"]),
        train_rows=tuple(_parse_row(row) for row in payload["train_rows"]),
        validation_rows=tuple(_parse_row(row) for row in payload["validation_rows"]),
        digest=payload["digest"],
    )
    if _digest_rows(snapshot.train_rows, snapshot.validation_rows) != snapshot.digest:
        raise ValueError("dataset digest mismatch")
    return snapshot
