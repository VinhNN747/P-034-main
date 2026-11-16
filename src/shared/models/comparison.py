from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base
from src.shared.models.core import utc_now


class ComparisonRun(Base):
    __tablename__ = "comparison_runs"
    __table_args__ = (
        UniqueConstraint("application_id", "idempotency_key", name="uq_comparison_runs_app_idempotency"),
        Index("ix_comparison_runs_application_created", "application_id", "created_at"),
        Index("ix_comparison_runs_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    application_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    gateway_request_id: Mapped[str | None] = mapped_column(ForeignKey("gateway_requests.request_id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="playground")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="offline")
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    constraints_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    strategies_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reference_model_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    model_snapshot_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    rubric_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    quality_evaluation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evaluator_model_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    max_comparison_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComparisonBranch(Base):
    __tablename__ = "comparison_branches"
    __table_args__ = (
        UniqueConstraint("comparison_id", "branch_key", name="uq_comparison_branches_key"),
        Index("ix_comparison_branches_comparison", "comparison_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    comparison_id: Mapped[str] = mapped_column(ForeignKey("comparison_runs.id"), nullable=False)
    branch_key: Mapped[str] = mapped_column(String(100), nullable=False)
    branch_type: Mapped[str] = mapped_column(String(20), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    usage_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pricing_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    input_price_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_price_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    pricing_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    queue_delay_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComparisonAttempt(Base):
    __tablename__ = "comparison_attempts"
    __table_args__ = (Index("ix_comparison_attempts_branch", "branch_id", "attempt_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    branch_id: Mapped[str] = mapped_column(ForeignKey("comparison_branches.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    usage_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ComparisonEvaluation(Base):
    __tablename__ = "comparison_evaluations"
    __table_args__ = (UniqueConstraint("comparison_id", name="uq_comparison_evaluations_run"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    comparison_id: Mapped[str] = mapped_column(ForeignKey("comparison_runs.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    evaluator_model_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evaluator_model_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    rubric_version: Mapped[str] = mapped_column(String(50), nullable=False, default="default-v1")
    scores_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rationale_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    usage_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "ComparisonAttempt",
    "ComparisonBranch",
    "ComparisonEvaluation",
    "ComparisonRun",
]
