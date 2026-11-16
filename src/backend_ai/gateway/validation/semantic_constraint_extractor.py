from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from pydantic import BaseModel

from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.llm.client import StructuredModelClient
from src.shared.dtos.gateway import GatewayRequest, ModelUsage, QualityPriority, RequestConstraints


class SemanticConstraintProposal(BaseModel):
    """Only supported gateway constraints may be proposed by a model."""

    max_cost_usd: float | None = None
    max_latency_ms: int | None = None
    quality_priority: QualityPriority | None = None
    # OpenAI Structured Outputs does not accept JSON Schema's ``uniqueItems``.
    # Pydantic emits that keyword for ``set`` fields, so the provider-facing
    # schema uses arrays and ``validate_proposal`` normalizes them through the
    # domain ``RequestConstraints`` model afterwards.
    allowed_models: list[str] | None = None
    blocked_models: list[str] | None = None
    allowed_providers: list[str] | None = None
    blocked_providers: list[str] | None = None
    output_language: str | None = None
    output_format: str | None = None
    max_output_tokens: int | None = None
    external_provider_allowed: bool | None = None
    allow_partial_response: bool | None = None


@dataclass
class ConstraintExtractionOutcome:
    values: dict[str, object]
    strategy: str
    confidence: float = 0.7
    model: str | None = None
    usage: ModelUsage = field(default_factory=ModelUsage)
    fallback: bool = False
    error: str | None = None
    usage_complete: bool = True


class BaseSemanticConstraintExtractor(ABC):
    @abstractmethod
    async def extract(self, request: GatewayRequest) -> ConstraintExtractionOutcome:
        raise NotImplementedError


def validate_proposal(proposal: SemanticConstraintProposal, config: GatewayConfig) -> dict[str, object]:
    values = proposal.model_dump(exclude_none=True)
    constraints = RequestConstraints.model_validate(values)
    if constraints.max_cost_usd is not None and constraints.max_cost_usd <= 0:
        raise ValueError("max_cost_usd must be positive")
    if constraints.max_latency_ms is not None and constraints.max_latency_ms <= 0:
        raise ValueError("max_latency_ms must be positive")
    if constraints.max_output_tokens is not None and constraints.max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be positive")
    if "output_format" in values and constraints.output_format not in config.supported_output_formats:
        raise ValueError(f"Unsupported output format: {constraints.output_format}")
    if constraints.allowed_models is not None and constraints.allowed_models & constraints.blocked_models:
        raise ValueError("Model allow/block sets overlap")
    if constraints.allowed_models is not None and not (constraints.allowed_models - constraints.blocked_models):
        raise ValueError("No eligible model remains")
    if constraints.allowed_providers is not None and constraints.allowed_providers & constraints.blocked_providers:
        raise ValueError("Provider allow/block sets overlap")
    if constraints.allowed_providers is not None and not (
        constraints.allowed_providers - constraints.blocked_providers
    ):
        raise ValueError("No eligible provider remains")
    return constraints.model_dump(include=set(values), exclude_none=True)


class LLMConstraintExtractor(BaseSemanticConstraintExtractor):
    """Structured semantic extraction; every proposal is validated before resolution."""

    def __init__(self, client: StructuredModelClient, config: GatewayConfig | None = None) -> None:
        self.client = client
        self.config = config or GatewayConfig()

    async def extract(self, request: GatewayRequest) -> ConstraintExtractionOutcome:
        prompt = (
            "Extract only constraints that are semantically implied but not captured by simple literal rules. "
            "Do not invent budgets, providers, models, formats, or permissions. Return null for absent fields.\n\n"
            f"REQUEST:\n{request.constraint_text()}"
        )
        feedback = ""
        usage = ModelUsage()
        errors: list[str] = []
        usage_complete = True
        for _attempt in range(self.config.llm_max_retries + 1):
            try:
                response = await self.client.generate(
                    prompt + feedback,
                    SemanticConstraintProposal,
                    self.config.understander_model,
                    self.config.llm_timeout_seconds,
                )
                usage.input_tokens += response.usage.input_tokens
                usage.output_tokens += response.usage.output_tokens
                usage_complete = usage_complete and response.usage_complete
                proposal = SemanticConstraintProposal.model_validate(response.data)
                values = validate_proposal(proposal, self.config)
                return ConstraintExtractionOutcome(
                    values=values,
                    strategy="llm-structured",
                    model=response.model,
                    usage=usage,
                    usage_complete=usage_complete,
                )
            except Exception as exc:
                usage_complete = False
                errors.append(str(exc))
                feedback = f"\nValidation failed: {exc}. Return corrected supported constraint fields only."
        return ConstraintExtractionOutcome(
            values={},
            strategy="llm-structured->rules-only",
            model=self.config.understander_model,
            usage=usage,
            usage_complete=usage_complete,
            fallback=True,
            error="; ".join(errors),
        )
