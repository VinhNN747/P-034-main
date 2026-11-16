from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.agents.routing.base import RouteDecision
from src.backend_ai.gateway.routing.session_affinity import SessionAffinity
from src.backend_ai.gateway.routing.tier_policy import TierDecision, TierPolicy, assign_tiers
from src.shared.core.model_tiers import tier_for_label
from src.shared.core.pricing import pricing_rules_from_metadata
from src.shared.dtos.gateway import Difficulty, QualityPriority, RequestConstraints, SubTask
from src.shared.models.core import Application
from src.shared.models.gateway import (
    ApplicationGatewayConfig,
    Model,
    ModelPool,
    ModelPoolMember,
    Provider,
    RoutingPolicy,
)
from src.shared.repositories.routing_catalog import RoutingSet, is_system_pool, routing_set

# Older catalog records use the broad "text" capability for general-purpose
# LLMs, while the rule understander emits the more precise code_generation
# requirement for Python/code tasks. Keep those records routable without
# weakening strict requirements such as vision or web_search.
_CAPABILITY_ALIASES: dict[str, frozenset[str]] = {
    "code_generation": frozenset({"code_generation", "coding", "text"}),
    "reasoning": frozenset({"reasoning", "text"}),
}


def supports_text_generation(model: Model) -> bool:
    """Exclude embedding-only catalog entries from chat/generation candidates."""
    capabilities = {str(value).strip().lower() for value in (model.capabilities_json or [])}
    return "embedding" not in capabilities or bool(
        capabilities.intersection({"chat", "text", "completion", "generation"})
    )


class RoutingError(Exception):
    pass


@dataclass(frozen=True)
class _Candidate:
    model: Model
    provider: Provider

    @property
    def cost(self) -> float:
        rules = pricing_rules_from_metadata(self.model.feature_metadata_json)
        if rules is not None and rules.long_context_input_threshold is not None:
            # Routing does not yet have an exact Anthropic count_tokens result.
            # Use the highest disclosed tier rather than under-ranking a long
            # prompt from a rough character estimate.
            return float(rules.long_context_input_per_million + rules.long_context_output_per_million)
        return self.model.input_price_per_million + self.model.output_price_per_million

    @property
    def latency(self) -> int:
        return self.model.target_latency_ms if self.model.target_latency_ms is not None else 2**31 - 1


def resolve_latency_overrides(candidates, latency_features, latency_context) -> dict[str, float]:
    """Resolve candidate estimates once, keeping diagnostics in the request scope."""
    if not candidates or not latency_features or latency_context is None or latency_context.resolver is None:
        return {}
    from src.backend_ai.latency.contracts import LatencyResolution

    now = next(iter(latency_features.values())).observed_at
    overrides: dict[str, float] = {}
    for item in candidates:
        features = latency_features.get(item.model.id)
        if features is None:
            continue
        resolution = latency_context.resolver.resolve(
            features,
            catalog_ms=item.model.target_latency_ms,
            now=now,
        )
        evidence = dict(resolution.evidence)
        evidence["catalog_ms"] = item.model.target_latency_ms
        latency_context.resolutions_by_model[item.model.id] = LatencyResolution(
            resolution.effective_ms,
            resolution.predicted_ms,
            resolution.source,
            resolution.gate_reason,
            resolution.model_version,
            evidence,
        )
        if latency_context.collector is not None:
            reasons = latency_context.collector.resolution_reasons
            reasons[resolution.gate_reason] = reasons.get(resolution.gate_reason, 0) + 1
            metric = "prediction_applied" if resolution.source == "predicted" else "prediction_fallback"
            latency_context.collector.metrics[metric] += 1
        logger.debug(
            "adaptive latency candidate resolved",
            extra={
                "latency_model_key": features.model_key.digest(),
                "latency_profile": features.profile_key,
                "latency_source": resolution.source,
                "latency_gate_reason": resolution.gate_reason,
                "latency_model_version": resolution.model_version,
                "latency_catalog_ms": item.model.target_latency_ms,
                "latency_predicted_ms": resolution.predicted_ms,
                "latency_effective_ms": resolution.effective_ms,
            },
        )
        if resolution.source == "predicted" and resolution.effective_ms is not None:
            overrides[item.model.id] = resolution.effective_ms
    return overrides


@dataclass(frozen=True)
class DatabaseRoutingSnapshot:
    config: ApplicationGatewayConfig
    policy: RoutingPolicy | None
    candidates: tuple[tuple[ModelPoolMember | None, Model, Provider], ...]
    # System pool for auto-routing (default or task template), read with the snapshot.
    routing: RoutingSet | None = None


# TierPolicy tier -> the ``_tier_quality`` scale used by ``difficulty_fit``.
_TIER_QUALITY = {"small_fast": 1.0, "mid": 2.0, "large_reasoning": 3.0}
logger = logging.getLogger(__name__)


class DatabaseRoutingService:
    """Resolve one DB-backed model choice for each decomposed task.

    Subtasks carrying ``routing_signals`` (jev or rule scores) get a target
    tier from TierPolicy; that tier replaces ``subtask.difficulty`` as the
    desired quality in the multi-factor ranking below.
    """

    def __init__(
        self,
        session: AsyncSession,
        application_id: str,
        available_provider_ids: set[str] | None = None,
        snapshot: DatabaseRoutingSnapshot | None = None,
        tier_policy: TierPolicy | None = None,
        shadow_policy: TierPolicy | None = None,
        affinity: SessionAffinity | None = None,
    ):
        self.session = session
        self.application_id = application_id
        self.available_provider_ids = available_provider_ids
        self.snapshot = snapshot
        self.tier_policy = tier_policy or TierPolicy()
        # Decides a tier for the same signals only to log it next to the real
        # decision (e.g. the learned model); it never affects routing.
        self.shadow_policy = shadow_policy
        # Reuses the conversation's previous model when this turn stays in its tier.
        self.affinity = affinity

    async def route(
        self,
        subtask: SubTask,
        constraints: RequestConstraints | None = None,
        *,
        latency_features: dict[str, object] | None = None,
    ) -> RouteDecision:
        constraints = constraints or RequestConstraints()
        config = (
            self.snapshot.config
            if self.snapshot
            else await self.session.scalar(
                select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == self.application_id)
            )
        )
        if config is None:
            raise RoutingError("Gateway configuration not found")

        if config.model_pool_id and self.snapshot is None:
            pool = await self.session.get(ModelPool, config.model_pool_id)
            app = await self.session.get(Application, self.application_id)
            if (
                pool is None
                or pool.status != "active"
                or app is None
                or (pool.owner_user_id != app.owner_user_id and not is_system_pool(pool))
            ):
                raise RoutingError("Active model pool not found")
        policy = (
            self.snapshot.policy
            if self.snapshot
            else await self.session.get(RoutingPolicy, config.routing_policy_id)
            if config.routing_policy_id
            else None
        )
        if config.routing_policy_id and (policy is None or policy.status != "active"):
            raise RoutingError("Active routing policy not found")
        routing = (self.snapshot.routing if self.snapshot else None) or (
            RoutingSet(list(self.snapshot.candidates))
            if self.snapshot
            else await routing_set(self.session, config.model_pool_id)
        )
        rows = self.snapshot.candidates if self.snapshot else routing.rows
        candidates = [
            _Candidate(model=model, provider=provider)
            for member, model, provider in rows
            if self._eligible(
                model,
                provider,
                subtask,
                constraints,
                self.available_provider_ids,
            )
        ]
        if not candidates:
            raise RoutingError("No routing candidate satisfies capabilities and constraints")
        own = routing.own_ids
        roles = {
            model.id: member.role
            for member, model, _ in rows
            if member is not None and (own is None or model.id in own)
        }
        pool_note = ""
        if routing.auto_roles:
            # Auto-routing on a system ladder (default pool or a task template). A request
            # that rules out any rung of it (a pinned model, an allow-list, a capability the
            # ladder lacks) keeps the application's own set, as a pinned model on OpenRouter does.
            in_auto = [item for item in candidates if item.model.id in routing.auto_roles]
            primaries = {model_id for model_id, role in routing.auto_roles.items() if role == "primary"}
            if primaries and primaries <= {item.model.id for item in in_auto}:
                candidates, roles, pool_note = in_auto, routing.auto_roles, routing.note
            elif own is not None:
                candidates = [item for item in candidates if item.model.id in own] or candidates
        else:
            pool_note = routing.note

        quality_scores = ((policy.guardrails_json if policy else {}) or {}).get("quality_scores", {})
        tier_decision: TierDecision | None = None
        tier_quality_override: dict[str, float] | None = None
        tier_source = ""
        shadow: dict | None = None
        if subtask.routing_signals is not None:
            tier_decision = self.tier_policy.decide(
                subtask.routing_signals,
                task_type=subtask.task_type,
                quality_priority=constraints.quality_priority,
            )
            shadow = self._shadow(subtask, constraints, tier_decision)
            # Place pool models in tiers by their quality_tier labels, or by
            # price when the pool is unlabelled (every model "standard"),
            # so the policy tier still separates cheap from strong models.
            tiers, tier_source = assign_tiers(
                [(item.model.id, item.model.quality_tier, item.cost) for item in candidates]
            )
            tier_quality_override = {model_id: _TIER_QUALITY[tier] for model_id, tier in tiers.items()}
        # Capacity before difficulty (LiteLLM's context-window escalation): a model whose
        # context window cannot hold what the executor will send is never a candidate, so
        # a long conversation or document moves to the nearest tier that fits. Tiers were
        # assigned above on the full eligible set, so this filter does not reshuffle them.
        candidates, context_note = self._fit_context(candidates, subtask, constraints)
        latency_overrides: dict[str, float] = {}
        latency_context = None
        if latency_features:
            from src.backend_ai.latency.context import current_latency_context

            latency_context = current_latency_context()
        latency_overrides = resolve_latency_overrides(candidates, latency_features, latency_context)
        candidates = self._rank_candidates(
            candidates,
            subtask,
            constraints.quality_priority,
            quality_scores,
            desired_quality=_TIER_QUALITY[tier_decision.tier] if tier_decision else None,
            tier_quality_override=tier_quality_override,
            latency_overrides=latency_overrides,
        )
        candidates = self._primary_first(candidates, roles, tier_quality_override)
        affinity_note = ""
        if self.affinity is not None and tier_quality_override is not None and self.affinity.claim(subtask.task_id):
            # The tier is decided again on every turn; only the model inside the tier sticks,
            # and only while the previous model is still eligible for this request. The
            # store is updated after execution, with the model that actually answered.
            previous = self.affinity.previous_model()
            kept = next((item for item in candidates if item.model.id == previous), None)
            if (
                kept is not None
                and kept is not candidates[0]
                and (tier_quality_override[kept.model.id] == tier_quality_override[candidates[0].model.id])
            ):
                candidates = [kept, *(item for item in candidates if item is not kept)]
                affinity_note = f"; session affinity kept {kept.model.name}"
        selected = candidates[0]
        candidate_by_id = {item.model.id: item for item in candidates}
        fallback_ids: list[str] = []
        fallback_provider_names: dict[str, str] = {}
        fallback_provider_kinds: dict[str, str] = {}
        fallback_provider_models: dict[str, str] = {}
        fallback_input_prices: dict[str, float] = {}
        fallback_output_prices: dict[str, float] = {}
        fallback_pricing_estimated: dict[str, bool] = {}

        def add_fallback(candidate: _Candidate) -> None:
            model_id = candidate.model.id
            if model_id == selected.model.id or model_id in fallback_ids:
                return
            fallback_ids.append(model_id)
            fallback_provider_names[model_id] = candidate.provider.name
            fallback_provider_kinds[model_id] = candidate.provider.kind
            fallback_provider_models[model_id] = candidate.model.name
            fallback_input_prices[model_id] = candidate.model.input_price_per_million
            fallback_output_prices[model_id] = candidate.model.output_price_per_million
            fallback_pricing_estimated[model_id] = True

        # Keep fallback candidates eligible for this task and ordered by the
        # same multi-factor ranking. Explicit policy/config fallbacks are
        # appended afterwards, so one stale fallback entry cannot force every
        # transient failure back to a single model such as gpt-4o-mini.
        if config.fallback_enabled:
            for candidate in candidates[1:]:
                add_fallback(candidate)
        for model_id in (policy.fallback_chain_json if policy else []) or []:
            candidate = candidate_by_id.get(model_id)
            if candidate and config.fallback_enabled:
                add_fallback(candidate)
        for model_id in config.fallback_model_ids_json or []:
            candidate = candidate_by_id.get(model_id)
            if candidate and config.fallback_enabled:
                add_fallback(candidate)
        if config.fallback_model_id and config.fallback_enabled:
            candidate = candidate_by_id.get(config.fallback_model_id)
            if candidate:
                add_fallback(candidate)

        return RouteDecision(
            task_id=subtask.task_id,
            tier=selected.model.id,
            strategy="database",
            selected_model=selected.model.id,
            provider_name=selected.provider.name,
            provider_kind=selected.provider.kind,
            provider_model=selected.model.name,
            input_price_per_million=selected.model.input_price_per_million,
            output_price_per_million=selected.model.output_price_per_million,
            pricing_version=selected.model.pricing_version,
            pricing_estimated=True,
            fallback_models=fallback_ids,
            fallback_provider_names=fallback_provider_names,
            fallback_provider_kinds=fallback_provider_kinds,
            fallback_provider_models=fallback_provider_models,
            fallback_input_prices_per_million=fallback_input_prices,
            fallback_output_prices_per_million=fallback_output_prices,
            fallback_pricing_estimated=fallback_pricing_estimated,
            confidence=subtask.routing_signals.confidence if subtask.routing_signals else 1.0,
            reason=(
                f"{tier_decision.explain()}; tiers by {tier_source}; picked {selected.model.name}"
                f"{context_note}{self._escalation_note(tier_decision, tier_quality_override, selected, context_note)}"
                f"{affinity_note}{pool_note}"
                if tier_decision
                else None
            ),
            shadow=shadow,
        )

    # Characters per token used to size a request against context windows. Deliberately low
    # (Vietnamese runs about 3 characters per token, English about 4), so a model is left out
    # a little early rather than failing on a prompt that does not fit.
    @staticmethod
    def _primary_first(
        candidates: list[_Candidate],
        roles: dict[str, str],
        tiers: dict[str, float] | None,
    ) -> list[_Candidate]:
        """A pool's primary model goes before the fallbacks of its tier, so the
        chain is primary -> same-tier fallback -> next tier. Ranking keeps each
        tier contiguous; without tiers, fallbacks go after every primary."""
        if not any(role == "fallback" for role in roles.values()):
            return candidates
        group: dict[float, int] = {}
        for index, item in enumerate(candidates):
            group.setdefault(tiers[item.model.id] if tiers else 0.0, index)
        return sorted(
            candidates,
            key=lambda item: (
                group[tiers[item.model.id] if tiers else 0.0],
                roles.get(item.model.id) == "fallback",
            ),
        )

    _CHARS_PER_TOKEN = 3

    @classmethod
    def _fit_context(
        cls, candidates: list[_Candidate], subtask: SubTask, constraints: RequestConstraints
    ) -> tuple[list[_Candidate], str]:
        """Drop models whose context window cannot hold the request plus its output budget.

        A model without a declared window is kept (unknown is not too small). If no model
        provably fits, the widest ones are kept so the provider reports the real error.
        """
        signals = subtask.routing_signals
        if signals is None or not signals.context_chars:
            return candidates, ""
        needed = -(-signals.context_chars // cls._CHARS_PER_TOKEN) + (constraints.max_output_tokens or 0)
        fitting_ids = {
            item.model.id
            for item in candidates
            if item.model.context_window is None or item.model.context_window >= needed
        }
        if len(fitting_ids) == len(candidates):
            return candidates, ""
        if not fitting_ids:
            widest = max(item.model.context_window or 0 for item in candidates)
            kept = [item for item in candidates if (item.model.context_window or 0) == widest]
            return kept, f"; context ~{needed} tokens exceeds every model, kept the widest ({widest})"
        dropped = sorted({item.model.name for item in candidates if item.model.id not in fitting_ids})
        kept = [item for item in candidates if item.model.id in fitting_ids]
        return kept, f"; context ~{needed} tokens excluded {', '.join(dropped)}"

    @staticmethod
    def _escalation_note(
        decision: TierDecision | None,
        tier_quality: dict[str, float] | None,
        selected: _Candidate,
        context_note: str,
    ) -> str:
        """Flag a pick outside the decided tier that the context filter forced."""
        if not context_note or decision is None or tier_quality is None:
            return ""
        if tier_quality.get(selected.model.id) == _TIER_QUALITY[decision.tier]:
            return ""
        return f"; context escalated from {decision.tier}"

    def _shadow(self, subtask: SubTask, constraints: RequestConstraints, actual: TierDecision) -> dict | None:
        """The shadow policy's tier for the same request, for comparison logs.
        A shadow failure is recorded and never breaks routing."""
        if self.shadow_policy is None:
            return None
        try:
            decision = self.shadow_policy.decide(
                subtask.routing_signals, task_type=subtask.task_type, quality_priority=constraints.quality_priority
            )
        except Exception as exc:  # noqa: BLE001 - shadow mode must not affect routing
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}
        model = getattr(self.shadow_policy, "model", None)
        return {
            "policy": getattr(model, "version", type(self.shadow_policy).__name__),
            "tier": decision.tier,
            "rule": decision.rule,
            "agrees": decision.tier == actual.tier,
            "actual_tier": actual.tier,
            "reason": decision.explain()[:300],
        }

    @staticmethod
    def _eligible(
        model: Model,
        provider: Provider,
        subtask: SubTask,
        constraints: RequestConstraints,
        available_provider_ids: set[str] | None = None,
    ) -> bool:
        if model.status != "active" or provider.status != "active":
            return False
        if provider.purpose != "target":
            return False
        if not supports_text_generation(model):
            return False
        if available_provider_ids is not None and provider.id not in available_provider_ids:
            return False
        if not constraints.external_provider_allowed and provider.kind not in {"offline", "local"}:
            return False
        model_keys = {model.id, model.name}
        provider_keys = {provider.id, provider.name, provider.kind}
        if constraints.allowed_models is not None and not model_keys.intersection(constraints.allowed_models):
            return False
        if model_keys.intersection(constraints.blocked_models):
            return False
        if constraints.allowed_providers is not None and not provider_keys.intersection(constraints.allowed_providers):
            return False
        if provider_keys.intersection(constraints.blocked_providers):
            return False
        available_capabilities = {str(capability).strip().lower() for capability in (model.capabilities_json or [])}
        for required_capability in subtask.required_capabilities:
            accepted_capabilities = _CAPABILITY_ALIASES.get(
                required_capability.strip().lower(), frozenset({required_capability.strip().lower()})
            )
            if not accepted_capabilities.intersection(available_capabilities):
                return False
        if constraints.max_latency_ms is not None and (
            model.target_latency_ms is None or model.target_latency_ms > constraints.max_latency_ms
        ):
            return False
        return True

    @staticmethod
    def _capability_score(candidate: _Candidate, subtask: SubTask) -> float:
        """Score native capability matches without making them an absolute override.

        Eligibility remains strict: every requirement must match either a native
        capability or an allowed compatibility alias. Native matches receive full
        credit while alias matches receive half credit, allowing an objective such
        as cost-first or latency-first to trade a small capability advantage for a
        materially cheaper or faster eligible model.
        """
        if not subtask.required_capabilities:
            return 1.0
        capabilities = {str(value).strip().lower() for value in (candidate.model.capabilities_json or [])}
        scores = []
        for required in subtask.required_capabilities:
            normalized = required.strip().lower()
            scores.append(1.0 if normalized in capabilities else 0.5)
        return sum(scores) / len(scores)

    @classmethod
    def _rank_candidates(
        cls,
        candidates: list[_Candidate],
        subtask: SubTask,
        priority: QualityPriority,
        quality_scores: dict,
        *,
        desired_quality: float | None = None,
        tier_quality_override: dict[str, float] | None = None,
        latency_overrides: dict[str, float] | None = None,
    ) -> list[_Candidate]:
        """Rank eligible models with an objective-specific multi-factor score.

        Every objective considers capability, quality, cost and latency. The objective controls their weights instead of replacing
        all other signals with one lexicographic rule.
        """
        weights = {
            QualityPriority.COST_FIRST: {
                "cost": 0.50,
                "capability": 0.20,
                "latency": 0.15,
                "quality": 0.10,
            },
            QualityPriority.LATENCY_FIRST: {
                "latency": 0.55,
                "capability": 0.20,
                "cost": 0.10,
                "quality": 0.10,
            },
            QualityPriority.QUALITY_FIRST: {
                "quality": 0.70,
                "capability": 0.15,
                "difficulty_fit": 0.075,
                "cost": 0.035,
                "latency": 0.025,
            },
            QualityPriority.BALANCED: {
                "difficulty_fit": 0.35,
                "quality": 0.20,
                "capability": 0.20,
                "cost": 0.10,
                "latency": 0.10,
            },
        }[priority]

        quality_by_id = {item.model.id: cls._quality_score(item, quality_scores) for item in candidates}
        tier_quality_by_id = tier_quality_override or {item.model.id: cls._tier_quality(item) for item in candidates}
        costs = [item.cost for item in candidates]
        latency_by_id = latency_overrides or {}
        latencies = [float(latency_by_id.get(item.model.id, item.latency)) for item in candidates]
        qualities = list(quality_by_id.values())
        if desired_quality is None:
            desired_quality = {
                Difficulty.EASY: 1.0,
                Difficulty.MEDIUM: 2.0,
                Difficulty.HARD: 3.0,
            }[subtask.difficulty]

        def score(item: _Candidate) -> float:
            signals = {
                "capability": cls._capability_score(item, subtask),
                "quality": cls._normalized_goodness(quality_by_id[item.model.id], qualities),
                "difficulty_fit": max(
                    0.0,
                    1.0 - abs(tier_quality_by_id[item.model.id] - desired_quality) / 2.0,
                ),
                "cost": cls._normalized_goodness(item.cost, costs, lower_is_better=True),
                "latency": cls._normalized_goodness(
                    float(latency_by_id.get(item.model.id, item.latency)), latencies, lower_is_better=True
                ),
            }
            return sum(weights[name] * signals[name] for name in weights)

        if tier_quality_override is not None:
            # TierPolicy already applied the caller's objective (cost/latency
            # first step down, high risk and attachment floors hold), so its
            # tier comes first: nearest tier wins, a tie goes to the stronger
            # tier, and the objective score only orders models inside it.
            def tier_key(item: _Candidate) -> tuple:
                tier_quality = tier_quality_by_id[item.model.id]
                return (abs(tier_quality - desired_quality), tier_quality < desired_quality)

            return sorted(candidates, key=lambda item: (*tier_key(item), -score(item), item.model.id))
        return sorted(candidates, key=lambda item: (-score(item), item.model.id))

    @staticmethod
    def _tier_quality(candidate: _Candidate) -> float:
        return _TIER_QUALITY[tier_for_label(candidate.model.quality_tier) or "mid"]

    @classmethod
    def _quality_score(cls, candidate: _Candidate, quality_scores: dict) -> float:
        return float(
            quality_scores.get(
                candidate.model.id,
                quality_scores.get(candidate.model.name, cls._tier_quality(candidate)),
            )
        )

    @staticmethod
    def _normalized_goodness(
        value: float,
        population: list[float],
        *,
        lower_is_better: bool = False,
    ) -> float:
        low = min(population)
        high = max(population)
        if high == low:
            return 1.0
        normalized = (value - low) / (high - low)
        return 1.0 - normalized if lower_is_better else normalized
