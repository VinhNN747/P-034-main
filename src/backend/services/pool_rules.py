"""Model pool rules: one primary model per tier, optional fallbacks.

Market routers route over a short ladder of models (LiteLLM: one model per
tier; Bedrock: two models of one family), so a pool holds exactly three
primary models, one each in the simple / mid / hard tiers of the catalog
``quality_tier`` labels. Fallback members are tried before moving to another
tier. See docs/routing-design-decisions.md sections 4 and 5.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.shared.config import Settings
from src.shared.models.gateway import Model, Provider

TIER_LABELS = ("simple", "mid", "hard")
# The pool of an offline pipeline: managed only by OfflinePipelineService, hidden from the pool APIs.
OFFLINE_POOL_PURPOSE = "offline_pipeline"
TIER_NAMES = {"simple": "small", "mid": "mid", "hard": "large"}
MAX_FALLBACKS = 6


class PoolRuleError(ValueError):
    """The member set breaks the pool rules; the message is shown to the user."""


@dataclass(frozen=True)
class PoolMember:
    role: str
    model: Model
    provider: Provider


def callable_provider_ids(
    settings: Settings,
    configured_provider_ids: set[str],
    provider_kinds: dict[str, str],
    local_endpoint_ids: set[str] | frozenset[str] = frozenset(),
) -> set[str]:
    """Connections callable by this AI backend, based on credential metadata only.

    ``local_endpoint_ids`` are self-hosted servers (``local`` with a base URL): callable in both modes.
    """
    if settings.gateway_execution_mode == "offline":
        allowed_kinds = {"offline", "local"}
    else:
        allowed_kinds = set()
        if settings.gateway_openai_enabled:
            allowed_kinds.add("openai")
        if settings.gateway_gemini_enabled:
            allowed_kinds.add("google")
        if settings.gateway_openrouter_enabled:
            allowed_kinds.add("openrouter")
        if settings.gateway_anthropic_enabled:
            allowed_kinds.add("anthropic")
    return {
        provider_id
        for provider_id in configured_provider_ids
        if provider_kinds.get(provider_id) in allowed_kinds or provider_id in local_endpoint_ids
    }


def is_callable(model: Model, provider: Provider, provider_ids: set[str]) -> bool:
    return (
        model.status == "active"
        and provider.status == "active"
        and provider.purpose == "target"
        and provider.id in provider_ids
        and model.health_status != "unhealthy"
    )


def validate(members: list[PoolMember]) -> None:
    """Enforced on create and edit. Pools saved before the rule keep routing."""
    primaries = [item for item in members if item.role == "primary"]
    fallbacks = [item for item in members if item.role == "fallback"]
    if len(primaries) != 3:
        raise PoolRuleError(
            f"A pool needs exactly 3 primary models, one per tier (small, mid, large); got {len(primaries)}."
        )
    tiers = [item.model.quality_tier for item in primaries]
    off_ladder = [item.model.name for item in primaries if item.model.quality_tier not in TIER_LABELS]
    if off_ladder:
        raise PoolRuleError(f"Primary models must have a small, mid or large tier label: {', '.join(off_ladder)}.")
    if sorted(tiers) != sorted(TIER_LABELS):
        raise PoolRuleError("The 3 primary models must be in 3 different tiers: one small, one mid, one large.")
    if len(fallbacks) > MAX_FALLBACKS:
        raise PoolRuleError(f"A pool can have at most {MAX_FALLBACKS} fallback models.")
    unlabelled = [item.model.name for item in fallbacks if item.model.quality_tier not in TIER_LABELS]
    if unlabelled:
        raise PoolRuleError(f"Fallback models must have a small, mid or large tier label: {', '.join(unlabelled)}.")


def warnings(members: list[PoolMember], provider_ids: set[str]) -> list[str]:
    """Warn, never block (doc section 5)."""
    notes: list[str] = []
    try:
        validate(members)
    except PoolRuleError as exc:
        notes.append(
            f"Created before the 3-tier rule: {exc} Until it is fixed, auto-routing for its "
            "applications uses the system default pool; models pinned by name still use this pool."
        )
    primaries = {item.model.quality_tier: item for item in members if item.role == "primary"}
    ladder = [primaries[tier] for tier in TIER_LABELS if tier in primaries]
    for lower, higher in zip(ladder, ladder[1:], strict=False):
        if _output_price(lower.model) > _output_price(higher.model):
            notes.append(
                f"{lower.model.name} ({TIER_NAMES[lower.model.quality_tier]}) costs more than "
                f"{higher.model.name} ({TIER_NAMES[higher.model.quality_tier]})."
            )
    # Vendors, not catalog rows: two rows of kind "openai" share one API and key.
    providers = {item.provider.kind for item in ladder}
    if len(providers) > 1:
        notes.append(
            "The primary models come from different providers (" + ", ".join(sorted(providers)) + "); "
            "a ladder from one family behaves more predictably."
        )
    blocked = [item.model.name for item in members if not is_callable(item.model, item.provider, provider_ids)]
    if blocked:
        notes.append("Cannot be called right now (inactive, unhealthy or no provider key): " + ", ".join(blocked) + ".")
    return notes


def suggest_fallbacks(
    primaries: list[PoolMember],
    catalog: list[tuple[Model, Provider]],
    provider_ids: set[str],
    *,
    allow_other_providers: bool,
) -> list[tuple[PoolMember, tuple[Model, Provider] | None, str]]:
    """One fallback per primary: same tier on another provider (vendor) when
    allowed, else same tier on the same provider; closest output price first.
    The same model through another catalog row is not a fallback. When a tier
    has nothing callable, the next tier up in the pool is the fallback."""
    taken = {item.model.id for item in primaries}
    taken_names = {item.model.name for item in primaries}
    result = []
    for primary in primaries:
        tier = primary.model.quality_tier
        options = [
            (model, provider)
            for model, provider in catalog
            if model.id not in taken
            and model.name not in taken_names
            and model.quality_tier == tier
            and is_callable(model, provider, provider_ids)
        ]
        other = [row for row in options if row[1].kind != primary.provider.kind]
        same = [row for row in options if row[1].kind == primary.provider.kind]
        ordered = (other + same) if allow_other_providers else same
        if not ordered:
            result.append((primary, None, "no other callable model in this tier; the next tier up is used"))
            continue
        price = _output_price(primary.model)
        best_group = other if allow_other_providers and other else same
        pick = min(best_group, key=lambda row: abs(_output_price(row[0]) - price))
        taken.add(pick[0].id)
        taken_names.add(pick[0].name)
        reason = "same tier, other provider" if pick[1].kind != primary.provider.kind else "same tier, same provider"
        result.append((primary, pick, reason))
    return result


def _output_price(model: Model) -> float:
    return float(model.output_price_per_million or 0.0)
