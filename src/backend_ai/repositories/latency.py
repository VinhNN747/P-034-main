from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.latency.contracts import (
    LatencyArtifact,
    LatencyFeatures,
    LatencyObservation,
    ModelKey,
    thaw,
)
from src.shared.models.latency import LatencyModelState, LatencyObservationRecord, LatencyTrainingRunRecord


@dataclass(frozen=True, slots=True)
class TrainingLease:
    owner: str
    token: str
    fencing_generation: int
    expires_at: datetime


def artifact_to_dict(artifact: LatencyArtifact) -> dict[str, Any]:
    return {
        "schema_version": artifact.schema_version,
        "version": artifact.version,
        "feature_names": list(artifact.feature_names),
        "coefficients": list(artifact.coefficients),
        "intercept": artifact.intercept,
        "numeric_means": thaw(artifact.numeric_means),
        "numeric_scales": thaw(artifact.numeric_scales),
        "categories": thaw(artifact.categories),
        "support": thaw(artifact.support),
        "validation_report": thaw(artifact.validation_report),
        "checksum": artifact.checksum,
    }


def artifact_from_dict(payload: Mapping[str, Any] | None) -> LatencyArtifact | None:
    if payload is None:
        return None
    return LatencyArtifact(
        schema_version=str(payload["schema_version"]),
        version=str(payload["version"]),
        feature_names=tuple(str(item) for item in payload["feature_names"]),
        coefficients=tuple(float(item) for item in payload["coefficients"]),
        intercept=float(payload["intercept"]),
        numeric_means=payload.get("numeric_means", {}),
        numeric_scales=payload.get("numeric_scales", {}),
        categories={key: tuple(value) for key, value in payload.get("categories", {}).items()},
        support=payload.get("support", {}),
        validation_report=payload.get("validation_report", {}),
        checksum=str(payload.get("checksum", "")),
    )


def observation_to_dict(observation: LatencyObservation) -> dict[str, Any]:
    key = observation.features.model_key
    return {
        "attempt_id": observation.attempt_id,
        "request_id": observation.request_id,
        "task_id": observation.task_id,
        "application_id": observation.application_id,
        "model_key_hash": key.digest(),
        "catalog_model_id": key.catalog_model_id,
        "provider_id": key.provider_id,
        "provider_kind": key.provider_kind,
        "upstream_model_name": key.upstream_model_name,
        "connection_fingerprint": key.connection_fingerprint,
        "profile_key": observation.features.profile_key,
        "feature_schema": observation.features.schema_version,
        "features_json": thaw(observation.features.values),
        "source": observation.source,
        "mode": observation.mode,
        "execution_mode": observation.execution_mode,
        "status": observation.status,
        "dispatched_at": observation.dispatched_at,
        "finished_at": observation.finished_at,
        "provider_total_ms": observation.provider_total_ms,
        "ttft_ms": observation.ttft_ms,
        "consumer_pause_ms": observation.consumer_pause_ms,
        "catalog_ms": observation.catalog_ms,
        "predicted_ms": observation.predicted_ms,
        "prediction_version": observation.prediction_version,
        "error_category": observation.error_category,
        "training_eligible": observation.training_eligible,
        "actual_payload_chars": observation.actual_payload_chars,
        "resolution_source": observation.resolution_source,
        "resolution_reason": observation.resolution_reason,
    }


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def observation_from_record(row: LatencyObservationRecord) -> LatencyObservation:
    key = ModelKey(
        row.catalog_model_id,
        row.provider_id,
        row.upstream_model_name,
        row.connection_fingerprint,
        row.provider_kind,
    )
    dispatched_at = _aware(row.dispatched_at)
    assert dispatched_at is not None
    features = LatencyFeatures(
        model_key=key,
        profile_key=row.profile_key,
        observed_at=dispatched_at,
        values=row.features_json,
        schema_version=row.feature_schema,
    )
    return LatencyObservation(
        attempt_id=row.attempt_id,
        request_id=row.request_id,
        task_id=row.task_id,
        application_id=row.application_id,
        features=features,
        source=row.source,
        mode=row.mode,
        dispatched_at=dispatched_at,
        status=row.status,
        finished_at=_aware(row.finished_at),
        provider_total_ms=row.provider_total_ms,
        ttft_ms=row.ttft_ms,
        consumer_pause_ms=row.consumer_pause_ms,
        catalog_ms=row.catalog_ms,
        predicted_ms=row.predicted_ms,
        prediction_version=row.prediction_version,
        error_category=row.error_category,
        execution_mode=row.execution_mode,
        training_eligible=row.training_eligible,
        actual_payload_chars=row.actual_payload_chars,
        resolution_source=row.resolution_source,
        resolution_reason=row.resolution_reason,
    )


class LatencyRepository:
    """Persistence boundary for best-effort attempt metrics and fenced models."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def insert_observations(self, rows: Sequence[LatencyObservation]) -> int:
        if not rows:
            return 0
        values = [observation_to_dict(row) for row in rows]
        dialect = self.session.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(LatencyObservationRecord).values(values).on_conflict_do_nothing(
                index_elements=["attempt_id"]
            )
            result = await self.session.execute(statement)
            inserted = max(0, int(result.rowcount or 0))
        elif dialect == "postgresql":
            statement = pg_insert(LatencyObservationRecord).values(values).on_conflict_do_nothing(
                index_elements=["attempt_id"]
            ).returning(LatencyObservationRecord.attempt_id)
            result = await self.session.execute(statement)
            inserted = len(result.scalars().all())
        else:
            statement = None
        if statement is None:
            inserted = 0
            for value in values:
                existing = await self.session.get(LatencyObservationRecord, value["attempt_id"])
                if existing is None:
                    self.session.add(LatencyObservationRecord(**value))
                    inserted += 1
        await self.session.commit()
        return inserted

    async def read_window(
        self,
        cutoff: datetime,
        *,
        days: int,
        limit: int,
    ) -> list[LatencyObservation]:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("cutoff must be timezone-aware")
        if days <= 0 or limit <= 0:
            raise ValueError("days and limit must be positive")
        lower = cutoff - timedelta(days=days)
        rows = await self.session.scalars(
            select(LatencyObservationRecord)
            .where(LatencyObservationRecord.dispatched_at >= lower)
            .where(LatencyObservationRecord.dispatched_at <= cutoff)
            .order_by(LatencyObservationRecord.dispatched_at.desc(), LatencyObservationRecord.attempt_id.desc())
            .limit(limit)
        )
        return [observation_from_record(row) for row in reversed(list(rows))]

    async def read_training_progress(self) -> dict[str, Any] | None:
        row = await self.session.scalar(
            select(LatencyTrainingRunRecord).order_by(
                LatencyTrainingRunRecord.created_at.desc(), LatencyTrainingRunRecord.run_id.desc()
            ).limit(1)
        )
        if row is None:
            return None
        return {
            "status": row.status,
            "created_at": _aware(row.created_at),
            "finished_at": _aware(row.finished_at) or _aware(row.created_at),
            "dataset_cutoff": _aware(row.dataset_cutoff),
            "sample_count": row.sample_count,
        }

    async def record_training_run(
        self,
        lease: TrainingLease,
        *,
        status: str,
        expected_active_generation: int,
        cutoff: datetime,
        sample_count: int,
        dataset_digest: str,
        report: Mapping[str, object],
        candidate_version: str | None = None,
        error_code: str | None = None,
        now: datetime,
    ) -> None:
        if status not in {"rejected", "failed", "abandoned"}:
            raise ValueError("terminal training status is invalid")
        row = await self.session.get(LatencyTrainingRunRecord, lease.token)
        values = {
            "status": status,
            "owner": lease.owner,
            "lease_token": lease.token,
            "fencing_generation": lease.fencing_generation,
            "expected_active_generation": expected_active_generation,
            "dataset_cutoff": cutoff,
            "sample_count": sample_count,
            "dataset_digest": dataset_digest,
            "candidate_version": candidate_version,
            "report_json": thaw(report),
            "error_code": error_code,
            "finished_at": now,
        }
        if row is None:
            self.session.add(LatencyTrainingRunRecord(run_id=lease.token, created_at=now, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await self.session.commit()

    async def start_training_run(
        self,
        lease: TrainingLease,
        *,
        expected_active_generation: int,
        cutoff: datetime,
        sample_count: int,
        now: datetime,
    ) -> None:
        self.session.add(
            LatencyTrainingRunRecord(
                run_id=lease.token,
                status="running",
                owner=lease.owner,
                lease_token=lease.token,
                fencing_generation=lease.fencing_generation,
                expected_active_generation=expected_active_generation,
                dataset_cutoff=cutoff,
                sample_count=sample_count,
                dataset_digest="",
                feature_schema="latency-v1",
                report_json={},
                created_at=now,
            )
        )
        await self.session.commit()

    async def claim_training(self, owner: str, now: datetime, ttl_seconds: int) -> TrainingLease | None:
        if ttl_seconds <= 0 or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("claim requires a positive TTL and timezone-aware timestamp")
        token = str(uuid4())
        expires_at = now + timedelta(seconds=ttl_seconds)
        result = await self.session.execute(
            update(LatencyModelState)
            .where(LatencyModelState.scope == "generation-v1")
            .where(or_(LatencyModelState.lease_expires_at.is_(None), LatencyModelState.lease_expires_at <= now))
            .values(
                lease_owner=owner,
                lease_token=token,
                lease_expires_at=expires_at,
                lease_generation=LatencyModelState.lease_generation + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
            .returning(LatencyModelState.lease_generation)
        )
        row = result.first()
        await self.session.commit()
        if row is None:
            return None
        return TrainingLease(owner, token, int(row[0]), expires_at)

    async def renew_training(self, lease: TrainingLease, now: datetime, ttl_seconds: int = 120) -> bool:
        if now.tzinfo is None or now.utcoffset() is None or ttl_seconds <= 0:
            raise ValueError("renew requires a positive TTL and timezone-aware timestamp")
        result = await self.session.execute(
            update(LatencyModelState)
            .where(LatencyModelState.scope == "generation-v1")
            .where(LatencyModelState.lease_token == lease.token)
            .where(LatencyModelState.lease_generation == lease.fencing_generation)
            .where(LatencyModelState.lease_expires_at > now)
            .values(lease_expires_at=now + timedelta(seconds=ttl_seconds), updated_at=now)
            .execution_options(synchronize_session="fetch")
        )
        await self.session.commit()
        return bool(result.rowcount)

    async def release_training(self, lease: TrainingLease) -> bool:
        result = await self.session.execute(
            update(LatencyModelState)
            .where(LatencyModelState.scope == "generation-v1")
            .where(LatencyModelState.lease_token == lease.token)
            .where(LatencyModelState.lease_generation == lease.fencing_generation)
            .values(lease_owner=None, lease_token=None, lease_expires_at=None)
            .execution_options(synchronize_session="fetch")
        )
        await self.session.commit()
        return bool(result.rowcount)

    async def publish(
        self,
        lease: TrainingLease,
        artifact: LatencyArtifact,
        report: Mapping[str, object],
        expected_active_generation: int,
        now: datetime,
    ) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        artifact_json = artifact_to_dict(artifact)
        if len(json.dumps(artifact_json, separators=(",", ":")).encode()) > 2 * 1024 * 1024:
            raise ValueError("artifact exceeds 2 MiB")
        current = await self.session.scalar(
            select(LatencyModelState).where(LatencyModelState.scope == "generation-v1")
        )
        if current is None:
            return False
        previous = list(current.previous_versions_json or [])
        if current.active_artifact_json is not None and current.active_version:
            previous.insert(0, {"version": current.active_version, "artifact": current.active_artifact_json})
        previous = previous[:5]
        run_id = lease.token
        result = await self.session.execute(
            update(LatencyModelState)
            .where(LatencyModelState.scope == "generation-v1")
            .where(LatencyModelState.lease_token == lease.token)
            .where(LatencyModelState.lease_generation == lease.fencing_generation)
            .where(LatencyModelState.lease_expires_at > now)
            .where(LatencyModelState.active_generation == expected_active_generation)
            .values(
                active_generation=LatencyModelState.active_generation + 1,
                active_version=artifact.version,
                active_artifact_json=artifact_json,
                active_run_id=run_id,
                previous_versions_json=previous,
                training_watermark=now,
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
        )
        if not result.rowcount:
            await self.session.rollback()
            return False
        run_record = await self.session.get(LatencyTrainingRunRecord, run_id)
        values = {
            "status": "published",
            "owner": lease.owner,
            "lease_token": lease.token,
            "fencing_generation": lease.fencing_generation,
            "expected_active_generation": expected_active_generation,
            "dataset_cutoff": now,
            "sample_count": int(report.get("sample_count", 0)),
            "dataset_digest": str(report.get("dataset_digest", "")),
            "feature_schema": artifact.schema_version,
            "candidate_version": artifact.version,
            "artifact_json": artifact_json,
            "report_json": thaw(report),
            "finished_at": now,
        }
        if run_record is None:
            self.session.add(LatencyTrainingRunRecord(run_id=run_id, created_at=now, **values))
        else:
            for key, value in values.items():
                setattr(run_record, key, value)
        await self.session.commit()
        return True

    async def read_active(self) -> tuple[int, LatencyArtifact | None]:
        state = await self.session.scalar(
            select(LatencyModelState).where(LatencyModelState.scope == "generation-v1")
        )
        if state is None:
            return 0, None
        return state.active_generation, artifact_from_dict(state.active_artifact_json)

    async def read_training_lease(self) -> tuple[str | None, datetime | None]:
        state = await self.session.scalar(
            select(LatencyModelState).where(LatencyModelState.scope == "generation-v1")
        )
        if state is None:
            return None, None
        return state.lease_token, _aware(state.lease_expires_at)

    async def rollback(self, expected_generation: int, previous_version: str) -> bool:
        state = await self.session.scalar(
            select(LatencyModelState).where(LatencyModelState.scope == "generation-v1")
        )
        if state is None:
            return False
        previous = list(state.previous_versions_json or [])
        selected = next((item for item in previous if item.get("version") == previous_version), None)
        if selected is None:
            return False
        new_previous = [item for item in previous if item.get("version") != previous_version]
        if state.active_version and state.active_artifact_json:
            new_previous.insert(0, {"version": state.active_version, "artifact": state.active_artifact_json})
        result = await self.session.execute(
            update(LatencyModelState)
            .where(LatencyModelState.scope == "generation-v1")
            .where(LatencyModelState.active_generation == expected_generation)
            .values(
                active_generation=LatencyModelState.active_generation + 1,
                active_version=selected["version"],
                active_artifact_json=selected["artifact"],
                previous_versions_json=new_previous[:5],
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session="fetch")
        )
        await self.session.commit()
        return bool(result.rowcount)

    async def delete_observations_before(self, cutoff: datetime, limit: int = 1000) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        ids = select(LatencyObservationRecord.attempt_id).where(
            LatencyObservationRecord.created_at < cutoff
        ).order_by(LatencyObservationRecord.created_at).limit(limit)
        result = await self.session.execute(
            delete(LatencyObservationRecord).where(LatencyObservationRecord.attempt_id.in_(ids))
        )
        await self.session.commit()
        return max(0, int(result.rowcount or 0))

    async def read_report_rows(self, since: datetime, limit: int = 20_000) -> list[LatencyObservation]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = await self.session.scalars(
            select(LatencyObservationRecord)
            .where(LatencyObservationRecord.dispatched_at >= since)
            .order_by(LatencyObservationRecord.dispatched_at, LatencyObservationRecord.attempt_id)
            .limit(limit)
        )
        return [observation_from_record(row) for row in rows]

    async def read_report_runs(self, since: datetime, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = await self.session.scalars(
            select(LatencyTrainingRunRecord)
            .where(LatencyTrainingRunRecord.created_at >= since)
            .order_by(LatencyTrainingRunRecord.created_at.desc(), LatencyTrainingRunRecord.run_id.desc())
            .limit(limit)
        )
        return [
            {
                "run_id": row.run_id,
                "status": row.status,
                "candidate_version": row.candidate_version,
                "sample_count": row.sample_count,
                "created_at": _aware(row.created_at),
                "finished_at": _aware(row.finished_at),
                "report": row.report_json or {},
            }
            for row in rows
        ]

    async def read_previous_artifacts(self) -> list[LatencyArtifact]:
        state = await self.session.scalar(
            select(LatencyModelState).where(LatencyModelState.scope == "generation-v1")
        )
        if state is None:
            return []
        artifacts = [artifact_from_dict(item.get("artifact")) for item in state.previous_versions_json or []]
        return [artifact for artifact in artifacts if artifact is not None]
