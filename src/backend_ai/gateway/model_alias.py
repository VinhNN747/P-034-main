"""Meaning of the OpenAI ``model`` field on the gateway's OpenAI-compatible API.

The OpenAI SDK requires ``model``, but here the gateway picks the model, so
the field becomes the routing control (same idea as Kong's ``model_alias``):

    auto          gateway routing with the application's configured objective
    auto:cheap    prefer cheaper tiers   (quality_priority=cost_first)
    auto:quality  prefer stronger tiers  (quality_priority=quality_first)
    auto:fast     prefer faster tiers, no task decomposition (latency_first)
    auto:code     route on the system coding pool (task template), like a
                  LiteLLM named auto-router or an OpenRouter preset. An
                  application whose own pool has a 3-tier ladder keeps its pool.
    <model name>  pin that model: becomes allowed_models=[name], so the
                  database router can only pick it (fallbacks are limited to
                  it too); a name outside the application's pool is rejected
                  by the runtime as model_not_found.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

QualityPriorityName = Literal["cost_first", "balanced", "latency_first", "quality_first"]


@dataclass(frozen=True)
class ModelSelection:
    requested: str
    quality_priority: QualityPriorityName | None = None
    pinned_model: str | None = None
    # System task template (pool) for auto-routing, e.g. "code".
    template: str | None = None

    @property
    def is_pinned(self) -> bool:
        return self.pinned_model is not None


ROUTING_ALIASES: dict[str, ModelSelection] = {
    "auto": ModelSelection("auto"),
    "auto:cheap": ModelSelection("auto:cheap", "cost_first"),
    "auto:quality": ModelSelection("auto:quality", "quality_first"),
    "auto:fast": ModelSelection("auto:fast", "latency_first"),
    "auto:code": ModelSelection("auto:code", template="code"),
}

ALIAS_DESCRIPTIONS: dict[str, str] = {
    "auto": "Gateway routing with the application's configured objective.",
    "auto:cheap": "Gateway routing, prefer cheaper model tiers.",
    "auto:quality": "Gateway routing, prefer stronger model tiers.",
    "auto:fast": "Gateway routing on faster tiers without task decomposition.",
    "auto:code": "Gateway routing on the system coding pool (one model per tier, tuned for code).",
}


def resolve_model(model: str) -> ModelSelection:
    name = model.strip()
    alias = ROUTING_ALIASES.get(name.lower())
    if alias is not None:
        return alias
    return ModelSelection(name, pinned_model=name)


def template_for_alias(alias: object) -> str | None:
    """The task template a request's ``model`` alias selects, if any."""
    selection = ROUTING_ALIASES.get(alias.strip().lower()) if isinstance(alias, str) else None
    return selection.template if selection else None
