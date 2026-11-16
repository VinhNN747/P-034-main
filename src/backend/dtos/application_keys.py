from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ApplicationKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ApplicationKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked: bool


class ApplicationKeyCreated(ApplicationKeyRead):
    raw_key: str


class ApplicationKeyPage(BaseModel):
    items: list[ApplicationKeyRead]
    page: int
    page_size: int
    total: int
