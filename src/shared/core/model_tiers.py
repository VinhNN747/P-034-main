"""Model tiers shared by the admin service and routing.

A catalog model's ``quality_tier`` label places it in exactly one routing
tier. The catalog uses ``simple`` / ``mid`` / ``hard``; older labels still map:

    simple -> small_fast      mid -> mid      hard -> large_reasoning

Which label each model carries is a catalog decision (see
``docs/model-tier-classification.md``); nothing here re-classifies models.
"""

from __future__ import annotations

TIER_ORDER = ("small_fast", "mid", "large_reasoning")

_TIER_LABELS = {
    "small_fast": "small_fast",
    "simple": "small_fast",
    "small": "small_fast",
    "fast": "small_fast",
    "economy": "small_fast",
    "basic": "small_fast",
    "mid": "mid",
    "standard": "mid",
    "balanced": "mid",
    "large_reasoning": "large_reasoning",
    "hard": "large_reasoning",
    "large": "large_reasoning",
    "reasoning": "large_reasoning",
    "premium": "large_reasoning",
    "advanced": "large_reasoning",
}


def tier_for_label(label: str | None) -> str | None:
    return _TIER_LABELS.get((label or "").strip().lower())
