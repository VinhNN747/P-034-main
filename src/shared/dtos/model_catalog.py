from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.shared.dtos.model_features import ModelFeatureMetadata


class ModelPricingRead(BaseModel):
    input_per_million: float
    output_per_million: float
    version: str


class ModelCatalogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider_id: str
    provider: str
    name: str
    display_name: str
    status: str
    capabilities: list[str]
    context_window: int | None
    max_output_tokens: int | None = None
    feature_metadata: ModelFeatureMetadata | None = None
    target_latency_ms: int | None
    last_probe_latency_ms: float | None = None
    quality_tier: str
    pricing: ModelPricingRead
    health_status: str
    last_probe_at: datetime | None = None
    last_probe_credential_version: int | None = None
    used_by_count: int = Field(ge=0)


class ModelCatalogPage(BaseModel):
    items: list[ModelCatalogRead]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


ModelSortField = Literal[
    "name", "provider", "status", "quality_tier", "latency", "input_price", "output_price", "used_by"
]
SortDirection = Literal["asc", "desc"]


class ModelProbeRead(BaseModel):
    model_id: str
    status: str
    latency_ms: float
    checked_at: datetime
    credential_version: int | None = None
    failure_reason: str | None = None
