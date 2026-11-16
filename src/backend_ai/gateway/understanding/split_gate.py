"""Split gate: a separate jev call that only asks whether a request holds
parts of very different difficulty.

Splitting pays off when one part can run on a small model and another needs a
reasoning model; when every part sits in the same tier, one call to that tier
answers them all faster and cheaper (answer bench 2026-10-09: 33 of 34 split
requests had every subtask in one tier). The gate runs next to the understand
call, so it adds cost but no latency. A yes is only a hint: the pipeline then
scores each subtask with jev and keeps the split only when their tiers differ
by ``split_min_tier_gap`` (see RequestPipeline)."""

from __future__ import annotations

import asyncio
from typing import Protocol

from pydantic import BaseModel, Field

from src.backend_ai.gateway.core.circuit_breaker import CircuitBreaker, is_transient_error
from src.backend_ai.gateway.understanding.jev import STATE_PREAMBLE, fit_state
from src.shared.dtos.gateway import ModelUsage


class SplitGateDecision(BaseModel):
    gap_probability: float = Field(ge=0, le=1)
    model: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)


class SplitGateClient(Protocol):
    async def check(self, prompt: str) -> SplitGateDecision: ...


def build_split_gate_questions() -> dict[str, object]:
    from langchain_typesafe import Noul
    from langchain_typesafe.types import NoulCriteria

    return {
        "difficulty_gap": Noul(
            instructions=(
                "Does the LATEST request ask for two or more SEPARATE results whose difficulty differs "
                "greatly, so that one result could be produced by a small fast model while another needs "
                "a strong reasoning model? Easy parts: looking up or restating a well-known fact, "
                "translating or rewriting a short text, simple arithmetic, a greeting or formatting. Hard "
                "parts: multi-step math or a proof, expert medical, legal or financial analysis of a "
                "specific case, non-trivial code or algorithm design. Answer no when only one result is "
                "requested (steps, constraints and formatting of one answer are not separate results), "
                "and when every requested result has similar difficulty, whether all easy or all hard."
            ),
            criteria=NoulCriteria(
                true="At least one requested result is easy and another is hard.",
                false="One result, or several results of similar difficulty.",
            ),
        )
    }


class TypeSafeSplitGateClient:
    """Asks the split question through langchain-typesafe's ``TypeSafeClassifier``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "jev-latest",
        timeout_seconds: float = 30.0,
        classifier: object | None = None,
    ) -> None:
        if classifier is None:
            from langchain_typesafe import TypeSafeClassifier

            classifier = TypeSafeClassifier(api_key=api_key, model=model, timeout=timeout_seconds)
        self._classifier = classifier
        self._questions = build_split_gate_questions()

    async def check(self, prompt: str) -> SplitGateDecision:
        response = await self._classifier.ainvoke(
            {"state": f"{STATE_PREAMBLE}\n\n{prompt}", "questions": self._questions}
        )
        usage = getattr(response, "usage", None)
        return SplitGateDecision(
            gap_probability=round(float(response.nouls["difficulty_gap"].noul), 3),
            model=getattr(response, "model", None),
            usage=ModelUsage(
                input_tokens=(usage.input_tokens or 0) if usage else 0,
                output_tokens=(usage.output_tokens or 0) if usage else 0,
            ),
        )


class SplitGateOutcome(BaseModel):
    gap_probability: float | None = None
    """None when the gate did not answer: the request then stays one task."""
    model: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    error: str | None = None


class SplitGate:
    """Timeout and circuit breaker around the client. A failure never fails
    the request; it only means "do not split"."""

    # Pricing/trace id, the same as the understand call's.
    model_id = "jev"
    breaker_key = "typesafe:jev"

    def __init__(
        self,
        client: SplitGateClient,
        *,
        timeout_seconds: float = 2.0,
        max_state_chars: int = 40_000,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.max_state_chars = max_state_chars
        self.circuit_breaker = circuit_breaker

    async def check(self, routing_text: str) -> SplitGateOutcome:
        breaker = self.circuit_breaker
        if breaker is not None and not breaker.allow(self.breaker_key):
            return SplitGateOutcome(error="split gate skipped: circuit open after repeated failures")
        try:
            decision = await asyncio.wait_for(
                self.client.check(fit_state(routing_text, self.max_state_chars)), timeout=self.timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - the gate must not fail the request
            if breaker is not None:
                if is_transient_error(exc):
                    breaker.record_failure(self.breaker_key, error=f"{type(exc).__name__}: {exc}"[:300])
                else:
                    breaker.release_probe(self.breaker_key)
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                return SplitGateOutcome(error=f"split gate did not answer within {self.timeout_seconds}s")
            return SplitGateOutcome(error=f"split gate failed: {type(exc).__name__}: {exc}"[:500])
        if breaker is not None:
            breaker.record_success(self.breaker_key)
        return SplitGateOutcome(gap_probability=decision.gap_probability, model=self.model_id, usage=decision.usage)
