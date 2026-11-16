import pytest

from src.shared.core.pricing import ModelPricing, PricingRegistry
from src.shared.dtos.model_features import ModelPricingMetadata


def _entry(
    model_id: str,
    *,
    input_price: float,
    output_price: float,
    cache_5m: float,
    cache_1h: float,
    cache_read: float,
    long_input: float | None = None,
    long_output: float | None = None,
    threshold: int | None = None,
) -> ModelPricing:
    rules = ModelPricingMetadata(
        standard_input_per_million=input_price,
        standard_output_per_million=output_price,
        long_context_input_threshold=threshold,
        long_context_input_per_million=long_input,
        long_context_output_per_million=long_output,
        cache_write_5m_input_per_million=cache_5m,
        cache_write_1h_input_per_million=cache_1h,
        cache_read_input_per_million=cache_read,
    )
    return ModelPricing(
        model_id=model_id,
        provider="anthropic",
        input_price_per_million=input_price,
        output_price_per_million=output_price,
        pricing_version="anthropic-docs-2026-10-17",
        pricing_rules=rules,
    )


def test_haiku_long_context_boundary_changes_input_and_output_rates_together():
    registry = PricingRegistry(
        [
            _entry(
                "haiku",
                input_price=0.10,
                output_price=0.50,
                cache_5m=0.125,
                cache_1h=0.20,
                cache_read=0.01,
                threshold=100_000,
                long_input=0.50,
                long_output=2.50,
            )
        ]
    )

    below, _ = registry.calculate("haiku", 99_999, 1_000)
    at, _ = registry.calculate("haiku", 100_000, 1_000)
    above, _ = registry.calculate("haiku", 100_001, 1_000)

    assert below == pytest.approx(0.0104999)
    assert at == pytest.approx(0.0105)
    assert above == pytest.approx(0.0525005)


def test_cache_usage_is_removed_once_and_long_context_counts_cached_prompt_tokens():
    registry = PricingRegistry(
        [
            _entry(
                "haiku",
                input_price=0.10,
                output_price=0.50,
                cache_5m=0.125,
                cache_1h=0.20,
                cache_read=0.01,
                threshold=100_000,
                long_input=0.50,
                long_output=2.50,
            )
        ]
    )

    standard, estimated = registry.calculate(
        "haiku", 100_000, 1_000, cache_read_tokens=10_000, cache_write_5m_tokens=5_000,
        cache_write_1h_tokens=5_000,
    )
    long, _ = registry.calculate("haiku", 100_001, 1_000, cache_read_tokens=100_000)

    assert standard == pytest.approx(0.010225)
    assert long == pytest.approx(0.0075005)
    assert estimated is False


@pytest.mark.parametrize(
    ("model_id", "input_price", "cache_read", "expected"),
    [("opus", 4.0, 0.20, 0.0002), ("fable", 10.0, 0.25, 0.00025)],
)
def test_cache_read_uses_model_specific_catalog_rate(model_id, input_price, cache_read, expected):
    registry = PricingRegistry(
        [
            _entry(
                model_id,
                input_price=input_price,
                output_price=input_price * 5,
                cache_5m=input_price * 1.25,
                cache_1h=input_price * 2,
                cache_read=cache_read,
            )
        ]
    )

    cost, estimated = registry.calculate(model_id, 1_000, 0, cache_read_tokens=1_000)

    assert cost == pytest.approx(expected)
    assert estimated is False


def test_unknown_cache_write_ttl_uses_highest_write_rate_and_marks_estimated():
    registry = PricingRegistry(
        [
            _entry(
                "haiku",
                input_price=0.10,
                output_price=0.50,
                cache_5m=0.125,
                cache_1h=0.20,
                cache_read=0.01,
            )
        ]
    )

    cost, estimated = registry.calculate("haiku", 1_000, 0, cache_write_tokens=1_000)

    assert cost == pytest.approx(0.0002)
    assert estimated is True


def test_legacy_scalar_pricing_keeps_old_calculation_when_cache_evidence_is_present():
    registry = PricingRegistry(
        [
            ModelPricing(
                model_id="legacy",
                provider="openai",
                input_price_per_million=1,
                output_price_per_million=2,
                pricing_version="legacy",
            )
        ]
    )

    cost, estimated = registry.calculate(
        "legacy", 1_000, 500, cache_read_tokens=200, cache_write_tokens=100
    )

    assert cost == pytest.approx(0.002)
    assert estimated is False


def test_router_uses_highest_published_price_tier_without_an_exact_counter():
    from src.backend_ai.services.gateway_routing_service import _Candidate
    from src.shared.models.gateway import Model, Provider

    model = Model(
        id="haiku",
        provider_id="anthropic",
        name="claude-haiku-5-5",
        display_name="Claude Haiku 5.5",
        input_price_per_million=0.10,
        output_price_per_million=0.50,
        feature_metadata_json={
            "pricing": {
                "standard_input_per_million": 0.10,
                "standard_output_per_million": 0.50,
                "long_context_input_threshold": 100_000,
                "long_context_threshold_operator": ">",
                "long_context_input_per_million": 0.50,
                "long_context_output_per_million": 2.50,
                "cache_write_5m_input_per_million": 0.125,
                "cache_write_1h_input_per_million": 0.20,
                "cache_read_input_per_million": 0.01,
            }
        },
    )
    provider = Provider(id="anthropic", name="anthropic", display_name="Anthropic", kind="anthropic")

    assert _Candidate(model=model, provider=provider).cost == pytest.approx(3.0)
