from __future__ import annotations

import re
from typing import Any

from src.shared.dtos.gateway import (
    ConstraintEnforcement,
    ConstraintSource,
    EffectiveConstraints,
    GatewayRequest,
    RequestConstraints,
    ResolvedConstraint,
)

_SOURCE_PRIORITY = {
    ConstraintSource.PLATFORM_POLICY: 0,
    ConstraintSource.APPLICATION_POLICY: 1,
    ConstraintSource.REQUEST_METADATA: 2,
    ConstraintSource.EXPLICIT_PROMPT: 3,
    ConstraintSource.INFERRED_PROMPT: 4,
    ConstraintSource.SYSTEM_DEFAULT: 5,
}
_NUMERIC_CEILINGS = {"max_cost_usd", "max_latency_ms", "max_output_tokens"}
_ALLOWED_FIELDS = {"allowed_models", "allowed_providers"}
_BLOCKED_FIELDS = {"blocked_models", "blocked_providers"}
_SOFT_FIELDS = {"quality_priority"}


def _explicit_prompt_constraints(prompt: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    lower = prompt.lower()
    if re.search(r"(?:trả lời|phản hồi|viết).{0,25}(?:tiếng việt|vietnamese)", lower):
        values["output_language"] = "vi"
    elif re.search(r"(?:respond|answer|write).{0,25}(?:in english|english)", lower):
        values["output_language"] = "en"
    json_requested = any(
        re.search(pattern, lower)
        for pattern in (
            r"(?:trả về|output|return|respond|answer|write).{0,80}\bjson\b",
            r"(?:dưới dạng|ở dạng|theo định dạng|in the form of|as)\s+(?:a\s+|an\s+|một\s+)?(?:valid\s+|hợp lệ\s+)?json\b",
        )
    )
    if json_requested:
        values["output_format"] = "json"
    csv_requested = any(
        re.search(pattern, lower)
        for pattern in (
            r"(?:trả về|output|return|respond|answer|write).{0,80}\bcsv\b",
            r"(?:dưới dạng|ở dạng|theo định dạng|in the form of|as)\s+(?:a\s+|an\s+|một\s+)?(?:valid\s+|hợp lệ\s+)?csv\b",
        )
    )
    if csv_requested:
        values["output_format"] = "csv"
    word_limit = re.search(r"(?:không quá|tối đa|no more than|max(?:imum)?)\s+(\d+)\s+(?:từ|words?)", lower)
    if word_limit:
        values["max_output_tokens"] = int(word_limit.group(1))
    latency = re.search(r"(?:under|within|trong|dưới|tối đa)\s+(\d+)\s*(?:ms|milliseconds?)", lower)
    if latency:
        values["max_latency_ms"] = int(latency.group(1))
    cost = re.search(r"(?:under|within|dưới|tối đa|không quá)\s*\$\s*(\d+(?:\.\d+)?)", lower)
    if cost:
        values["max_cost_usd"] = float(cost.group(1))
    if re.search(r"(?:do not|don't|không).{0,25}(?:external provider|nhà cung cấp bên ngoài)", lower):
        values["external_provider_allowed"] = False
    return values


def _inferred_prompt_constraints(prompt: str) -> dict[str, Any]:
    lower = prompt.lower()
    vietnamese_markers = (" hãy ", " và ", " không ", " bằng tiếng ", "tóm tắt")
    padded = f" {lower} "
    if any(marker in padded for marker in vietnamese_markers):
        return {"output_language": "vi"}
    return {}


class ConstraintResolver:
    def __init__(self, defaults: RequestConstraints | None = None) -> None:
        self.defaults = defaults or RequestConstraints()

    @staticmethod
    def _present(model: RequestConstraints, include_defaults: bool = False) -> dict[str, Any]:
        data = model.model_dump()
        if include_defaults:
            return data
        return {key: data[key] for key in model.model_fields_set}

    def resolve(
        self,
        request: GatewayRequest,
        platform_policy: RequestConstraints | None = None,
        application_policy: RequestConstraints | None = None,
        semantic_constraints: dict[str, Any] | None = None,
        semantic_confidence: float = 0.7,
    ) -> EffectiveConstraints:
        candidates: dict[str, list[tuple[Any, ConstraintSource, float]]] = {}

        def add(values: dict[str, Any], source: ConstraintSource, confidence: float) -> None:
            for key, value in values.items():
                if value is not None:
                    candidates.setdefault(key, []).append((value, source, confidence))

        add(self._present(self.defaults, include_defaults=True), ConstraintSource.SYSTEM_DEFAULT, 1.0)
        add(_inferred_prompt_constraints(request.constraint_text()), ConstraintSource.INFERRED_PROMPT, 0.65)
        add(semantic_constraints or {}, ConstraintSource.INFERRED_PROMPT, semantic_confidence)
        add(_explicit_prompt_constraints(request.constraint_text()), ConstraintSource.EXPLICIT_PROMPT, 0.98)
        add(self._present(request.constraints), ConstraintSource.REQUEST_METADATA, 1.0)
        if application_policy:
            add(self._present(application_policy), ConstraintSource.APPLICATION_POLICY, 1.0)
        if platform_policy:
            add(self._present(platform_policy), ConstraintSource.PLATFORM_POLICY, 1.0)

        resolved: dict[str, ResolvedConstraint] = {}
        effective: dict[str, Any] = self.defaults.model_dump()
        conflicts: list[str] = []

        for key, choices in candidates.items():
            ordered = sorted(choices, key=lambda item: _SOURCE_PRIORITY[item[1]])
            sources = list(dict.fromkeys(item[1] for item in ordered))
            if key in _NUMERIC_CEILINGS:
                numeric = [(value, source, confidence) for value, source, confidence in ordered if value is not None]
                selected = min(numeric, key=lambda item: item[0])
            elif key in _ALLOWED_FIELDS:
                sets = [(set(value), source, confidence) for value, source, confidence in ordered if value is not None]
                value = set.intersection(*(entry[0] for entry in sets)) if sets else None
                selected = (value, sets[0][1], min(entry[2] for entry in sets)) if sets else ordered[0]
            elif key in _BLOCKED_FIELDS:
                sets = [(set(value), source, confidence) for value, source, confidence in ordered]
                selected = (set().union(*(entry[0] for entry in sets)), sets[0][1], min(entry[2] for entry in sets))
            else:
                selected = ordered[0]

            value, source, confidence = selected
            effective[key] = value
            enforcement = (
                ConstraintEnforcement.SOFT
                if key in _SOFT_FIELDS or source in {ConstraintSource.INFERRED_PROMPT, ConstraintSource.SYSTEM_DEFAULT}
                else ConstraintEnforcement.HARD
            )
            resolved[key] = ResolvedConstraint(
                value=value,
                source=source,
                enforcement=enforcement,
                confidence=confidence,
                contributing_sources=sources,
            )

        for noun, allowed_key, blocked_key in (
            ("models", "allowed_models", "blocked_models"),
            ("providers", "allowed_providers", "blocked_providers"),
        ):
            allowed = effective.get(allowed_key)
            blocked = set(effective.get(blocked_key) or [])
            if allowed is not None:
                remaining = set(allowed) - blocked
                effective[allowed_key] = remaining
                if allowed_key in resolved:
                    resolved[allowed_key].value = remaining
                if not remaining:
                    conflicts.append(f"No eligible {noun} remain after policy resolution")

        return EffectiveConstraints(
            constraints=RequestConstraints.model_validate(effective),
            resolved=resolved,
            conflicts=conflicts,
        )
