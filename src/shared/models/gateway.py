from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base
from src.shared.models.core import utc_now


class Provider(Base):
    __tablename__ = "providers"
    __table_args__ = (
        CheckConstraint("purpose IN ('target', 'internal')", name="ck_providers_purpose"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, default="target", server_default="target")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    secret_env_var: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    owner_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    """A customer's own endpoint (bring your own server): only that user's pools may route to
    it, and it never appears in the shared catalog. None = a system connection managed by admins."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class Model(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feature_metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    target_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_price_per_million: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    output_price_per_million: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pricing_version: Mapped[str] = mapped_column(String(50), nullable=False, default="1")
    quality_tier: Mapped[str] = mapped_column(String(30), nullable=False, default="standard", server_default="standard")
    health_status: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown", server_default="unknown")
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_probe_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_probe_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_probe_credential_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ModelPool(Base):
    __tablename__ = "model_pools"
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_model_pools_owner_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    purpose: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ModelPoolMember(Base):
    __tablename__ = "model_pool_members"
    __table_args__ = (UniqueConstraint("pool_id", "model_id", name="uq_pool_model"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    pool_id: Mapped[str] = mapped_column(ForeignKey("model_pools.id"), nullable=False)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), nullable=False)
    # "primary": the pool's model for its tier. "fallback": used when the primary of
    # its tier cannot serve the request, before moving to another tier.
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="primary", server_default="primary")


class RoutingPolicy(Base):
    __tablename__ = "routing_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    match_conditions_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    guardrails_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    fallback_chain_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ApplicationGatewayConfig(Base):
    __tablename__ = "application_gateway_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), unique=True, nullable=False)
    model_pool_id: Mapped[str | None] = mapped_column(ForeignKey("model_pools.id"), nullable=True)
    routing_policy_id: Mapped[str | None] = mapped_column(ForeignKey("routing_policies.id"), nullable=True)
    budget_ceiling: Mapped[float | None] = mapped_column(Float, nullable=True)
    routing_objective: Mapped[str] = mapped_column(
        String(50), nullable=False, default="balanced", server_default="balanced"
    )
    fallback_model_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    warning_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.8, server_default="0.8")
    budget_action: Mapped[str] = mapped_column(String(20), nullable=False, default="block", server_default="block")
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=800, server_default="800")
    max_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=30.0, server_default="30")
    retry_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    fallback_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fallback_model_id: Mapped[str | None] = mapped_column(ForeignKey("models.id"), nullable=True)
    default_understander: Mapped[str] = mapped_column(String(50), nullable=False, default="jev")
    default_decomposer: Mapped[str] = mapped_column(String(50), nullable=False, default="deterministic")
    default_router: Mapped[str] = mapped_column(String(50), nullable=False, default="database")
    default_aggregator: Mapped[str] = mapped_column(String(50), nullable=False, default="deterministic")
    # The classifier's tier thresholds for this application; NULL = GATEWAY_CLASSIFIER_*_DIFFICULTY.
    classifier_mid_difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    classifier_large_difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
