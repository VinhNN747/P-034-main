from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base
from src.shared.models.core import utc_now


class LatencyObservationRecord(Base):
    """One isolated provider attempt and its immutable pre-dispatch features."""

    __tablename__ = "latency_observations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'error', 'timeout', 'cancelled', 'incomplete')",
            name="ck_latency_observations_status",
        ),
        CheckConstraint("provider_total_ms IS NULL OR provider_total_ms > 0", name="ck_latency_observations_duration"),
        Index("ix_latency_observations_group_finished", "model_key_hash", "finished_at"),
        Index("ix_latency_observations_finished", "finished_at"),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    application_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_model_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    upstream_model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    connection_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_key: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_schema: Mapped[str] = mapped_column(String(30), nullable=False)
    features_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_total_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    consumer_pause_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    catalog_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    predicted_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    prediction_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    training_eligible: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="1")
    actual_payload_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class LatencyTrainingRunRecord(Base):
    __tablename__ = "latency_training_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'published', 'rejected', 'failed', 'abandoned')",
            name="ck_latency_training_runs_status",
        ),
        Index("ix_latency_training_runs_created", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(36), nullable=False)
    fencing_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_active_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dataset_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    feature_schema: Mapped[str] = mapped_column(
        String(30), nullable=False, default="latency-v1", server_default="latency-v1"
    )
    candidate_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    artifact_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    report_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LatencyModelState(Base):
    __tablename__ = "latency_model_state"
    __table_args__ = (
        CheckConstraint("scope = 'generation-v1'", name="ck_latency_model_state_scope"),
        Index("ix_latency_model_state_lease_expiry", "lease_expires_at"),
    )

    scope: Mapped[str] = mapped_column(String(30), primary_key=True, default="generation-v1")
    active_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    active_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    active_artifact_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    previous_versions_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    training_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
