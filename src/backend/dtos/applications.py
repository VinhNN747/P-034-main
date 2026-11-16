from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.backend.dtos.application_budget import ApplicationBudgetSummary
from src.backend.dtos.gateway_configuration import GatewayConfigRead, GatewayConfigUpsert
from src.shared.dtos.model_catalog import ModelCatalogRead

ApplicationEnvironment = Literal["development", "staging", "production"]
ApplicationStatus = Literal["active", "paused", "archived"]
ApplicationStatusInput = Literal["active", "paused"]
ApplicationSortField = Literal["name", "created_at", "updated_at", "environment", "status"]
SortDirection = Literal["asc", "desc"]


class ApplicationCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    environment: ApplicationEnvironment = "development"
    status: ApplicationStatusInput = "active"
    tags: list[str] = Field(default_factory=list, max_length=50)
    gateway_config: GatewayConfigUpsert | None = None


class ApplicationUpdate(BaseModel):
    slug: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    environment: ApplicationEnvironment | None = None
    status: ApplicationStatusInput | None = None
    tags: list[str] | None = Field(default=None, max_length=50)


class ApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slug: str
    name: str
    description: str | None
    owner_user_id: str
    environment: ApplicationEnvironment
    status: ApplicationStatus
    pipeline: Literal["online", "offline"] = "online"
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    gateway_config: GatewayConfigRead | None = None
    budget: ApplicationBudgetSummary | None = None
    request_count: int = 0
    config_version: int | None = None


class ApplicationModelPreview(BaseModel):
    id: str
    provider: str
    name: str
    display_name: str


class ApplicationListRead(BaseModel):
    id: str
    slug: str
    name: str
    owner_user_id: str
    is_owned_by_current_user: bool = False
    environment: ApplicationEnvironment
    status: ApplicationStatus
    created_at: datetime
    updated_at: datetime
    routing_objective: str = "balanced"
    budget_ceiling: float | None = None
    config_version: int | None = None
    model_preview: list[ApplicationModelPreview] = Field(default_factory=list)
    model_total: int = Field(default=0, ge=0)


class ApplicationPage(BaseModel):
    items: list[ApplicationListRead]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class ApplicationModelsRead(BaseModel):
    application_id: str
    total: int = Field(ge=0)
    items: list[ModelCatalogRead]
