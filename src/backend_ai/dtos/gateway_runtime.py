from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from src.shared.dtos.gateway import ChatMessage, GatewayRequest, RequestConstraints


class GatewayRuntimeInput(BaseModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=128)
    prompt: str | None = None
    messages: list[ChatMessage] | None = None
    context: dict = Field(default_factory=dict)
    constraints: RequestConstraints = Field(default_factory=RequestConstraints)
    metadata: dict = Field(default_factory=dict)
    source: Literal["playground", "api", "unknown"] = "api"
    # None = the application's configured default_understander.
    understander: str | None = None
    decomposer: str = "none"
    router: str = "database"
    aggregator: str = "auto"

    @model_validator(mode="after")
    def prompt_or_messages(self):
        if self.prompt is None and not self.messages:
            raise ValueError("prompt or messages is required")
        return self

    def to_gateway_request(self) -> GatewayRequest:
        request_id = self.request_id or str(uuid4())
        metadata = dict(self.metadata)
        metadata.update(
            {
                "understander": self.understander or "rule",
                "decomposer": self.decomposer,
                "router": self.router,
                "aggregator": self.aggregator,
                "source": self.source,
            }
        )
        return GatewayRequest(
            request_id=request_id,
            application_id=self.application_id,
            prompt=self.prompt,
            messages=self.messages,
            context=self.context,
            constraints=self.constraints,
            metadata=metadata,
        )


class GatewayRuntimeResponse(BaseModel):
    request_id: str
    application_id: str
    source: str
    status: str
    output: str | None = None
    provider: str | None = None
    model: str | None = None
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    fallback_used: bool = False
    fallback_reason: str | None = None
    trace_count: int = 0


class GatewayRequestDetailResponse(GatewayRuntimeResponse):
    aggregation_status: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    traces: list[dict[str, Any]] = Field(default_factory=list)
    executions: list[dict[str, Any]] = []
    aggregation: dict[str, Any] | None = None
    response: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
