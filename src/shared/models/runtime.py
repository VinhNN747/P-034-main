from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base
from src.shared.models.core import utc_now


class GatewayRequestRecord(Base):
    __tablename__ = "gateway_requests"
    __table_args__ = (
        Index("ix_gateway_requests_application_started", "application_id", "started_at"),
        Index("ix_gateway_requests_api_key_started", "application_api_key_id", "started_at"),
    )

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    application_id: Mapped[str] = mapped_column(String(36), nullable=False)
    application_api_key_id: Mapped[str | None] = mapped_column(ForeignKey("application_api_keys.id"), nullable=True)
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    messages_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    constraints_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    strategies_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown", server_default="unknown")
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_transaction_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    response_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    aggregation_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuntimeOperationRecord(Base):
    __tablename__ = "runtime_operations"
    __table_args__ = (
        UniqueConstraint("application_id", "idempotency_key_hash", name="uq_runtime_operations_app_key"),
        Index("ix_runtime_operations_application_started", "application_id", "started_at"),
        Index("ix_runtime_operations_application_status_expiry", "application_id", "status", "expires_at"),
    )

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), nullable=False)
    application_api_key_id: Mapped[str] = mapped_column(ForeignKey("application_api_keys.id"), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalization_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    public_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_body_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GatewayTaskRecord(Base):
    __tablename__ = "gateway_tasks"
    __table_args__ = (
        UniqueConstraint("request_id", "task_id", name="uq_gateway_tasks_request_task"),
        Index("ix_gateway_tasks_request", "request_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("gateway_requests.request_id"), nullable=False)
    task_id: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    difficulty: Mapped[str] = mapped_column(String(20), nullable=False)
    dependencies_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    input_from_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    required_capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    constraints_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    expected_output: Mapped[str] = mapped_column(Text, nullable=False)
    goal_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class RouteDecisionRecord(Base):
    __tablename__ = "route_decisions"
    __table_args__ = (
        UniqueConstraint("request_id", "task_id", name="uq_route_decisions_request_task"),
        Index("ix_route_decisions_model", "selected_model"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("gateway_requests.request_id"), nullable=False)
    task_id: Mapped[str] = mapped_column(String(80), nullable=False)
    strategy: Mapped[str] = mapped_column(String(80), nullable=False)
    selected_tier: Mapped[str | None] = mapped_column(String(150), nullable=True)
    selected_model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    fallback_models_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModelExecutionRecord(Base):
    __tablename__ = "model_executions"
    __table_args__ = (
        Index("ix_model_executions_request_task", "request_id", "task_id"),
        Index("ix_model_executions_provider_model", "provider", "model"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("gateway_requests.request_id"), nullable=False)
    task_id: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    selected_model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    fallback: Mapped[bool] = mapped_column(nullable=False, default=False)
    failure_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    result_payload: Mapped[str | None] = mapped_column(Text, nullable=True)


class AggregationResultRecord(Base):
    __tablename__ = "aggregation_results"
    __table_args__ = (UniqueConstraint("request_id", name="uq_aggregation_results_request"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("gateway_requests.request_id"), nullable=False)
    strategy: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    contributions_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    missing_goal_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class StageTraceRecord(Base):
    __tablename__ = "stage_traces"
    __table_args__ = (
        Index("ix_stage_traces_request_started", "request_id", "started_at"),
        Index("ix_stage_traces_stage_status", "stage", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("gateway_requests.request_id"), nullable=False)
    stage: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageEventRecord(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_request_created", "request_id", "created_at"),
        Index("ix_usage_events_app_created", "application_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("gateway_requests.request_id"), nullable=False)
    application_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    estimated: Mapped[bool] = mapped_column(nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown", server_default="unknown")
    billing_transaction_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class IncidentRecord(Base):
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    application_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
