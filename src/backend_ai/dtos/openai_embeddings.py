from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: StrictStr = Field(min_length=1)
    input: Any
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: StrictInt | None = None
    user: StrictStr | None = None

    @field_validator("dimensions", "user", mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("null is not supported for this parameter")
        return value

    @field_validator("user")
    @classmethod
    def bound_user(cls, value: str) -> str:
        if len(value) > 256:
            raise ValueError("user must contain at most 256 characters")
        return value


class EmbeddingData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object: Literal["embedding"] = "embedding"
    index: int
    embedding: list[float] | str


class EmbeddingUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_tokens: int
    total_tokens: int


class EmbeddingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object: Literal["list"] = "list"
    data: list[EmbeddingData]
    model: str
    usage: EmbeddingUsage
