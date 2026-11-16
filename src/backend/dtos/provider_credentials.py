from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ProviderKind = Literal["openai", "google", "openrouter", "anthropic", "typesafe", "offline", "local"]
ConnectionPurpose = Literal["target", "internal"]
ProviderStatus = Literal["active", "inactive"]
BindingPurpose = Literal["internal_openai", "ocr_google", "routing_jev"]


class ProviderConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    kind: ProviderKind
    purpose: ConnectionPurpose
    status: ProviderStatus = "active"
    base_url: str | None = Field(default=None, max_length=500)
    api_key: SecretStr | None = None

    @field_validator("name", "display_name")
    @classmethod
    def trim_nonempty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value must not be blank")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        secret = value.get_secret_value()
        if len(secret.strip()) < 5:
            raise ValueError("Credential must contain at least 5 characters")
        return value


class ProviderConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    status: ProviderStatus | None = None

    @field_validator("display_name")
    @classmethod
    def trim_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Display name must not be blank")
        return value

    @model_validator(mode="after")
    def require_update(self):
        if not self.model_fields_set or (self.display_name is None and self.status is None):
            raise ValueError("At least one supported field must be provided")
        return self


class ProviderCredentialReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr
    expected_version: int = Field(ge=0)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 5:
            raise ValueError("Credential must contain at least 5 characters")
        return value


class CredentialSummary(BaseModel):
    configured: bool
    status: Literal["active", "revoked"] | None = None
    version: int | None = None
    key_last4: str | None = None
    last_test_status: Literal["untested", "healthy", "failed", "unavailable", "skipped"] = "untested"
    last_test_at: datetime | None = None
    last_test_credential_version: int | None = None
    last_test_mode: str | None = None
    last_test_model: str | None = None
    last_test_error_code: str | None = None
    last_test_latency_ms: float | None = None


class ProviderConnectionRead(BaseModel):
    id: str
    name: str
    display_name: str
    kind: ProviderKind
    purpose: ConnectionPurpose
    status: ProviderStatus
    base_url: str | None
    credential: CredentialSummary
    created_at: datetime
    updated_at: datetime


class ServiceProviderBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str | None


class ServiceProviderBindingRead(BaseModel):
    purpose: BindingPurpose
    provider_id: str | None
    provider_name: str | None
    provider_kind: ProviderKind | None
    provider_status: ProviderStatus | None
    credential: CredentialSummary | None
