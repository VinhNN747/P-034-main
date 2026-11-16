from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiErrorResponse(BaseModel):
    """Stable, user-safe error payload for normalized API endpoints."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any] | None = None
    request_id: str | None = None


class PageMeta(BaseModel):
    """Stable pagination metadata shared by list endpoints."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)
