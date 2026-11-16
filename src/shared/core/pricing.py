from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.shared.dtos.gateway import OutputEvidence
from src.shared.dtos.model_features import ModelPricingMetadata


class ModelPricing(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    provider: str
    input_price_per_million: float = Field(ge=0)
    output_price_per_million: float = Field(ge=0)
    pricing_version: str
    capabilities: set[str] = Field(default_factory=set)
    estimated: bool = False
    pricing_rules: ModelPricingMetadata | None = None


class PricingRegistry:
    def __init__(self, entries: list[ModelPricing] | None = None) -> None:
        self._entries = {entry.model_id: entry for entry in entries or []}

    def register(self, entry: ModelPricing) -> None:
        self._entries[entry.model_id] = entry

    def get(self, model_id: str) -> ModelPricing | None:
        return self._entries.get(model_id)

    def calculate(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read_tokens: int = 0,
        cache_write_5m_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
        cache_write_tokens: int | None = None,
        force_highest_tier: bool = False,
    ) -> tuple[float, bool]:
        if input_tokens == 0 and output_tokens == 0 and not any(
            (cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens, cache_write_tokens or 0)
        ):
            return 0.0, False
        pricing = self.get(model_id)
        if pricing is None:
            return 0.0, True
        rules = pricing.pricing_rules
        if rules is None:
            cost = (
                input_tokens / 1_000_000 * pricing.input_price_per_million
                + output_tokens / 1_000_000 * pricing.output_price_per_million
            )
            return round(cost, 10), pricing.estimated

        threshold = rules.long_context_input_threshold
        use_long_context = force_highest_tier or (
            threshold is not None
            and (
                input_tokens > threshold
                if rules.long_context_threshold_operator == ">"
                else input_tokens >= threshold
            )
        )
        input_rate = (
            rules.long_context_input_per_million
            if use_long_context and rules.long_context_input_per_million is not None
            else rules.standard_input_per_million
        )
        output_rate = (
            rules.long_context_output_per_million
            if use_long_context and rules.long_context_output_per_million is not None
            else rules.standard_output_per_million
        )
        # Cache prices are multipliers over the input rate. Scaling the stored
        # standard cache rates by the selected input tier preserves those
        # modifiers for Haiku's long-context requests.
        tier_scale = (
            input_rate / rules.standard_input_per_million
            if rules.standard_input_per_million > 0
            else 1.0
        )
        read_rate = rules.cache_read_input_per_million * tier_scale
        write_5m_rate = rules.cache_write_5m_input_per_million * tier_scale
        write_1h_rate = rules.cache_write_1h_input_per_million * tier_scale

        cache_read = max(0, cache_read_tokens)
        write_5m = max(0, cache_write_5m_tokens)
        write_1h = max(0, cache_write_1h_tokens)
        estimated = pricing.estimated or force_highest_tier
        known_writes = write_5m + write_1h
        unknown_writes = 0
        if cache_write_tokens is not None:
            total_writes = max(0, cache_write_tokens)
            if total_writes > known_writes:
                unknown_writes = total_writes - known_writes
            elif total_writes < known_writes:
                # Inconsistent upstream counters: retain the larger known
                # duration breakdown and make the cost's uncertainty visible.
                estimated = True
        if unknown_writes:
            # A missing TTL must not understate usage; choose the more
            # expensive of the two configured cache-write rates.
            if write_5m_rate > write_1h_rate:
                write_5m += unknown_writes
            else:
                write_1h += unknown_writes
            estimated = True

        cached_total = cache_read + write_5m + write_1h
        if cached_total > input_tokens:
            # Invalid usage breakdown. Price all prompt tokens at the selected
            # base rate instead of allowing negative uncached usage or double
            # charging counters that exceed the provider's total.
            input_cost = input_tokens / 1_000_000 * input_rate
            estimated = True
        else:
            uncached = input_tokens - cached_total
            input_cost = (
                uncached / 1_000_000 * input_rate
                + cache_read / 1_000_000 * read_rate
                + write_5m / 1_000_000 * write_5m_rate
                + write_1h / 1_000_000 * write_1h_rate
            )
        output_cost = output_tokens / 1_000_000 * output_rate
        return round(input_cost + output_cost, 10), estimated

    def calculate_usage(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        evidence: OutputEvidence | Mapping[str, Any] | None = None,
        *,
        force_highest_tier: bool = False,
    ) -> tuple[float, bool]:
        """Calculate a receipt using normalized token evidence when available."""
        values = evidence.model_dump() if isinstance(evidence, OutputEvidence) else evidence or {}
        return self.calculate(
            model_id,
            input_tokens,
            output_tokens,
            cache_read_tokens=_counter(values.get("cached_tokens")),
            cache_write_5m_tokens=_counter(values.get("cache_write_5m_tokens")),
            cache_write_1h_tokens=_counter(values.get("cache_write_1h_tokens")),
            cache_write_tokens=_counter(values.get("cache_write_tokens")),
            force_highest_tier=force_highest_tier,
        )


def pricing_rules_from_metadata(metadata: object) -> ModelPricingMetadata | None:
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("pricing")
    return ModelPricingMetadata.model_validate(raw) if raw is not None else None


def _counter(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0
