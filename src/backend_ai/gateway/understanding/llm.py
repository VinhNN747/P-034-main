from __future__ import annotations

import json

from src.backend_ai.dtos.llm import LLMSemanticAnalysis
from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.llm.client import StructuredModelClient
from src.backend_ai.gateway.understanding.base import BaseUnderstander, UnderstandingOutcome
from src.backend_ai.gateway.understanding.rules import RuleUnderstander
from src.shared.dtos.gateway import EffectiveConstraints, GatewayRequest, ModelUsage, SemanticAnalysis


class LLMUnderstander(BaseUnderstander):
    name = "llm"

    def __init__(
        self,
        client: StructuredModelClient,
        config: GatewayConfig | None = None,
        fallback: BaseUnderstander | None = None,
    ) -> None:
        self.client = client
        self.config = config or GatewayConfig()
        self.fallback = fallback or RuleUnderstander()

    async def understand(self, request: GatewayRequest, constraints: EffectiveConstraints) -> UnderstandingOutcome:
        prompt = (
            "Analyze the gateway request using only the rubric fields in the supplied schema. "
            "Scores are ordinal 0..3, not subjective overall confidence. Do not decompose the request.\n\n"
            f"REQUEST:\n{request.routing_text()}\n\n"
            f"CONTEXT KEYS: {sorted(request.context)}\n"
            f"EFFECTIVE CONSTRAINTS: {json.dumps(constraints.constraints.model_dump(mode='json'), ensure_ascii=False)}"
        )
        feedback = ""
        usage = ModelUsage()
        errors: list[str] = []
        usage_complete = True
        for _attempt in range(self.config.llm_max_retries + 1):
            try:
                response = await self.client.generate(
                    prompt + feedback,
                    LLMSemanticAnalysis,
                    self.config.understander_model,
                    self.config.llm_timeout_seconds,
                )
                usage.input_tokens += response.usage.input_tokens
                usage.output_tokens += response.usage.output_tokens
                usage_complete = usage_complete and response.usage_complete
                analysis = SemanticAnalysis.model_validate(response.data)
                return UnderstandingOutcome(
                    analysis=analysis,
                    strategy=self.name,
                    model=response.model,
                    usage=usage,
                    usage_complete=usage_complete,
                )
            except Exception as exc:
                usage_complete = False
                errors.append(str(exc))
                feedback = f"\nPrevious output failed validation: {exc}. Return a corrected schema-valid result."

        fallback = await self.fallback.understand(request, constraints)
        fallback.fallback = True
        fallback.error = "; ".join(errors)
        fallback.strategy = f"{self.name}->{self.fallback.name}"
        fallback.model = self.config.understander_model
        fallback.usage = usage
        fallback.usage_complete = usage_complete
        return fallback
