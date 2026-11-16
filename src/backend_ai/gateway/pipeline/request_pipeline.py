from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from datetime import datetime
from time import perf_counter
from typing import Literal

from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.decomposition.base import BaseDecomposer
from src.backend_ai.gateway.decomposition.deterministic_decomposer import DeterministicGoalDecomposer
from src.backend_ai.gateway.decomposition.llm_decomposer import LLMDecomposer
from src.backend_ai.gateway.decomposition.no_decomposer import NoDecomposer
from src.backend_ai.gateway.decomposition.validator import DecompositionValidator
from src.backend_ai.gateway.multimodal.analyzer import AttachmentAnalyzer
from src.backend_ai.gateway.multimodal.digest import (
    attachment_routing_counts,
    execution_context,
    required_capabilities,
    routing_digest,
)
from src.backend_ai.gateway.observability.cost_tracker import summarize_costs
from src.backend_ai.gateway.observability.trace import TraceCollector, trace_timer
from src.backend_ai.gateway.routing.tier_policy import TIER_ORDER, TierPolicy
from src.backend_ai.gateway.scoring.decomposition_policy import DecompositionPolicy, promote_goals_confirmed_by_jev
from src.backend_ai.gateway.scoring.request_scorer import RequestScorer
from src.backend_ai.gateway.understanding.base import BaseUnderstander
from src.backend_ai.gateway.understanding.goal_extractor import DeterministicGoalExtractor, normalize_prompt
from src.backend_ai.gateway.understanding.jev import JevUnderstander
from src.backend_ai.gateway.understanding.llm import LLMUnderstander
from src.backend_ai.gateway.understanding.rules import RuleUnderstander
from src.backend_ai.gateway.understanding.split_gate import SplitGate, SplitGateOutcome
from src.backend_ai.gateway.understanding.task_classifier import ClassifierUnderstander, TaskClassifier
from src.backend_ai.gateway.validation.constraint_resolver import ConstraintResolver
from src.backend_ai.gateway.validation.request_validator import RequestValidator
from src.backend_ai.gateway.validation.semantic_constraint_extractor import BaseSemanticConstraintExtractor
from src.shared.core.pricing import PricingRegistry
from src.shared.dtos.gateway import (
    AttachmentAnalysis,
    CostSummary,
    DecompositionResult,
    Difficulty,
    EffectiveConstraints,
    GatewayRequest,
    GraphValidationStatus,
    IssueSeverity,
    PipelineResult,
    RequestConstraints,
    RoutingSignals,
    SemanticAnalysis,
    ValidationIssue,
)


class RequestPipeline:
    def __init__(
        self,
        *,
        config: GatewayConfig | None = None,
        pricing: PricingRegistry | None = None,
        understanders: dict[str, BaseUnderstander] | None = None,
        decomposers: dict[str, BaseDecomposer] | None = None,
        platform_policy: RequestConstraints | None = None,
        application_policies: dict[str, RequestConstraints] | None = None,
        semantic_constraint_extractor: BaseSemanticConstraintExtractor | None = None,
        attachment_analyzer: AttachmentAnalyzer | None = None,
        split_gate: SplitGate | None = None,
        tier_policy: TierPolicy | None = None,
        task_classifier_shadow: TaskClassifier | None = None,
    ) -> None:
        self.config = config or GatewayConfig()
        # Shadow mode: the local task classifier runs next to the selected understander and its
        # output is only written to the understand trace, to compare before routing on it.
        self.task_classifier_shadow = task_classifier_shadow
        # difficulty_gap mode: asks, next to the understand call, whether parts differ
        # greatly in difficulty; then each subtask's tier (same TierPolicy as routing)
        # decides whether the split is kept.
        self.split_gate = split_gate
        self.tier_policy = tier_policy or TierPolicy()
        self.pricing = pricing or PricingRegistry()
        self.understanders = dict(understanders) if understanders else {"rule": RuleUnderstander(), "jev": JevUnderstander()}
        # Always selectable (the offline pipeline maps jev/llm onto it); without a model it runs the rules.
        self.understanders.setdefault(ClassifierUnderstander.name, ClassifierUnderstander(None))
        self.decomposers = dict(decomposers) if decomposers is not None else {"none": NoDecomposer()}
        self.decomposers.setdefault("none", NoDecomposer())
        self.decomposers.setdefault("deterministic", DeterministicGoalDecomposer())
        self.platform_policy = platform_policy
        self.application_policies = application_policies or {}
        self.semantic_constraint_extractor = semantic_constraint_extractor
        self.attachment_analyzer = attachment_analyzer or AttachmentAnalyzer()
        self.request_validator = RequestValidator(self.config)
        self.goal_extractor = DeterministicGoalExtractor(self.config)
        self.constraint_resolver = ConstraintResolver()
        self.scorer = RequestScorer(self.config)
        self.policy = DecompositionPolicy(self.config)
        self.graph_validator = DecompositionValidator(self.config)
        self._result_cache: OrderedDict[str, PipelineResult] = OrderedDict()

    def register_llm_stages(
        self,
        understander: LLMUnderstander,
        decomposer: LLMDecomposer,
        constraint_extractor: BaseSemanticConstraintExtractor | None = None,
    ) -> None:
        self.understanders["llm"] = understander
        self.decomposers["llm"] = decomposer
        if constraint_extractor is not None:
            self.semantic_constraint_extractor = constraint_extractor

    async def run(
        self,
        request: GatewayRequest,
        *,
        understander: str = "rule",
        decomposer: str = "none",
    ) -> PipelineResult:
        if understander not in self.understanders:
            raise ValueError(f"Unknown understander '{understander}'. Available: {sorted(self.understanders)}")
        if decomposer not in self.decomposers:
            raise ValueError(f"Unknown decomposer '{decomposer}'. Available: {sorted(self.decomposers)}")

        traces = TraceCollector(request.request_id)
        request.set_routing_window(self.config.routing_window)
        started, timer = trace_timer()
        validation = self.request_validator.validate(request)
        traces.add(
            stage="request_validation",
            strategy="deterministic",
            started_at=started,
            timer_started=timer,
            status="success" if validation.accepted else "error",
            input_tokens=validation.estimated_input_tokens,
            metadata={"error_count": len(validation.errors), "warning_count": len(validation.warnings)},
        )
        if not validation.accepted:
            self._append_skipped(traces, "Request rejected before semantic processing")
            return PipelineResult(
                request=request, validation=validation, traces=traces.traces, costs=summarize_costs(traces.traces)
            )

        started, timer = trace_timer()
        goal_extraction = self.goal_extractor.extract(request.focus_text())
        traces.add(
            stage="goal_extraction",
            strategy=goal_extraction.extractor_version,
            started_at=started,
            timer_started=timer,
            metadata={
                "decision": goal_extraction.decision,
                "confidence": goal_extraction.confidence,
                "goal_count": len(goal_extraction.goals),
                "reason": goal_extraction.reason,
            },
        )

        cache_key = self._cache_key(request, understander, decomposer)
        cached = self._cache_get(cache_key, request)
        if cached is not None:
            return cached

        decision_understander = understander

        started, timer = trace_timer()
        extraction = None
        if decision_understander == "llm" and self.semantic_constraint_extractor is not None:
            extraction = await self.semantic_constraint_extractor.extract(request)
        effective = self.constraint_resolver.resolve(
            request,
            platform_policy=self.platform_policy,
            application_policy=self.application_policies.get(request.application_id),
            semantic_constraints=extraction.values if extraction else None,
            semantic_confidence=extraction.confidence if extraction else 0.7,
        )
        if effective.conflicts:
            for conflict in effective.conflicts:
                validation.errors.append(
                    ValidationIssue(
                        code="NO_ELIGIBLE_MODEL",
                        field="constraints",
                        message=conflict,
                        severity=IssueSeverity.ERROR,
                    )
                )
            validation.accepted = False
        constraint_cost, constraint_cost_estimated = self.pricing.calculate_usage(
            extraction.model if extraction and extraction.model else "offline",
            extraction.usage.input_tokens if extraction else 0,
            extraction.usage.output_tokens if extraction else 0,
            getattr(extraction, "output_evidence", None) if extraction else None,
        )
        traces.add(
            stage="constraint_resolution",
            strategy=(f"deterministic-precedence+{extraction.strategy}" if extraction else "deterministic-precedence"),
            model=extraction.model if extraction else None,
            started_at=started,
            timer_started=timer,
            input_tokens=extraction.usage.input_tokens if extraction else 0,
            output_tokens=extraction.usage.output_tokens if extraction else 0,
            cost_usd=constraint_cost,
            status="error" if effective.conflicts else "fallback" if extraction and extraction.fallback else "success",
            error="; ".join(effective.conflicts) or (extraction.error if extraction else None),
            metadata={
                "resolved_count": len(effective.resolved),
                "cost_estimated": constraint_cost_estimated,
                "semantic_model_call": extraction is not None,
                "usage_complete": extraction.usage_complete if extraction else True,
            },
        )
        if not validation.accepted:
            self._append_skipped(
                traces, "Resolved policies produced an empty candidate set", after="constraint_resolution"
            )
            return PipelineResult(
                request=request,
                validation=validation,
                goal_extraction=goal_extraction,
                effective_constraints=effective,
                traces=traces.traces,
                costs=summarize_costs(traces.traces),
            )

        attachments = await self._analyze_attachments(request, effective, traces)
        public_openai = str(request.metadata.get("client_metadata", {}).get("api_surface", "")).startswith("openai")
        unusable = [item for item in attachments if item.status != "analyzed" or item.error]
        if public_openai and unusable and not effective.constraints.allow_partial_response:
            validation.accepted = False
            validation.errors.append(ValidationIssue(code="ATTACHMENT_ANALYSIS_FAILED", field="messages",
                message="One or more attachments could not be analyzed.", severity=IssueSeverity.ERROR))
            self._append_skipped(traces, "Attachment analysis did not satisfy the public media policy")
            return PipelineResult(request=request, validation=validation, goal_extraction=goal_extraction,
                effective_constraints=effective, attachments=attachments, traces=traces.traces,
                costs=summarize_costs(traces.traces))

        gate_task = (
            asyncio.create_task(self._timed_split_gate(request.routing_text()))
            if self._split_gate_applies(request) and decision_understander != ClassifierUnderstander.name
            else None
        )
        shadow_task = (
            asyncio.create_task(self._shadow_classify(request.focus_text()))
            if self.task_classifier_shadow is not None and decision_understander != "classifier"
            else None
        )
        selected_understander = self.understanders[decision_understander]
        started, timer = trace_timer()
        try:
            understanding = await selected_understander.understand(request, effective)
        except BaseException:
            for task in (gate_task, shadow_task):
                if task is not None:
                    task.cancel()
            raise
        shadow_metadata = await shadow_task if shadow_task is not None else None
        understand_cost, estimated = self.pricing.calculate_usage(
            understanding.model or "offline", understanding.usage.input_tokens, understanding.usage.output_tokens,
            getattr(understanding, "output_evidence", None),
        )
        traces.add(
            stage="understand",
            strategy=understanding.strategy,
            model=understanding.model,
            started_at=started,
            timer_started=timer,
            input_tokens=understanding.usage.input_tokens,
            output_tokens=understanding.usage.output_tokens,
            cost_usd=understand_cost,
            status="fallback" if understanding.fallback else "success",
            error=understanding.error,
            metadata={
                "cost_estimated": estimated,
                "requested_understander": understander,
                "decision_understander": decision_understander,
                "usage_complete": understanding.usage_complete,
                **(understanding.details or {}),
                **({"task_classifier_shadow": shadow_metadata} if shadow_metadata is not None else {}),
            },
        )

        split_gap_probability = None
        if gate_task is not None:
            split_gap_probability = self._trace_split_gate(*await gate_task, traces)

        if goal_extraction.decision != "unclear":
            aligned = understanding.analysis.model_copy(deep=True)
            aligned.goal_count = len(goal_extraction.goals)
            if goal_extraction.decision == "single":
                aligned.task_independence = 0
                aligned.dependency_clarity = 3
            else:
                has_dependencies = any(goal.depends_on_goal_ids for goal in goal_extraction.goals)
                aligned.task_independence = 1 if has_dependencies else 3
                aligned.dependency_clarity = 3
            aligned.explanations.append(
                f"Goal count aligned to deterministic contract {goal_extraction.extractor_version}."
            )
            understanding.analysis = aligned

        if attachments:
            understanding.analysis = self._with_attachment_signals(understanding.analysis, attachments)

        started, timer = trace_timer()
        profile = self.scorer.score(request, understanding.analysis, effective)
        traces.add(
            stage="request_scoring",
            strategy="configured-weighted-rubric",
            started_at=started,
            timer_started=timer,
            metadata={"weights": self.config.weights.model_dump()},
        )

        # The classifier routes the whole request as one task on its difficulty (a rule fallback
        # reports another strategy and decomposes as usual).
        single_task = understanding.strategy == ClassifierUnderstander.name
        started, timer = trace_timer()
        planner_available = (
            decomposer == "llm"
            and "llm" in self.decomposers
            and not request.metadata.get("disable_decomposition")
            and not single_task
        )
        decision = self.policy.decide(
            request,
            profile,
            validation.estimated_input_tokens,
            effective,
            goal_extraction,
            understanding.analysis,
            planner_available=planner_available,
            split_gap_probability=split_gap_probability,
        )
        promoted = promote_goals_confirmed_by_jev(goal_extraction, decision, self.config.max_subtasks)
        if promoted is not None:
            goal_extraction = promoted
        profile.decomposition_recommended = decision.should_decompose
        profile.score_explanations.append(decision.reason)
        traces.add(
            stage="decomposition_decision",
            strategy="deterministic-policy",
            started_at=started,
            timer_started=timer,
            metadata={
                "decision": decision.should_decompose,
                "band": decision.band,
                "reason": decision.reason,
                "estimated_overhead_ratio": decision.estimated_overhead_ratio,
            },
        )

        decomposition_reason = decision.reason
        if request.metadata.get("disable_decomposition"):
            # Caller opted out (e.g. OpenAI model alias "auto:fast").
            selected_decomposer = self.decomposers["none"]
            decomposition_reason += "; decomposition disabled by request"
        elif single_task:
            selected_decomposer = self.decomposers["none"]
            decomposition_reason += "; classifier understander routes the request as one task"
        elif not decision.should_decompose:
            selected_decomposer = self.decomposers["none"]
        elif decomposer == "none":
            selected_decomposer = self.decomposers["deterministic"]
            decomposition_reason += "; deterministic goal decomposition selected"
        else:
            selected_decomposer = self.decomposers[decomposer]
        started, timer = trace_timer()
        decomposition_outcome = await selected_decomposer.decompose(
            request,
            understanding.analysis,
            profile,
            effective,
            decomposition_reason,
            goal_extraction,
        )
        decompose_cost, decompose_estimated = self.pricing.calculate_usage(
            decomposition_outcome.model or "offline",
            decomposition_outcome.usage.input_tokens,
            decomposition_outcome.usage.output_tokens,
            getattr(decomposition_outcome, "output_evidence", None),
        )
        traces.add(
            stage="decompose",
            strategy=decomposition_outcome.strategy,
            model=decomposition_outcome.model,
            started_at=started,
            timer_started=timer,
            input_tokens=decomposition_outcome.usage.input_tokens,
            output_tokens=decomposition_outcome.usage.output_tokens,
            cost_usd=decompose_cost,
            status="fallback" if decomposition_outcome.fallback else "success",
            error=decomposition_outcome.error,
            metadata={
                "cost_estimated": decompose_estimated,
                "usage_complete": decomposition_outcome.usage_complete,
                "goal_source": "llm-planner" if decomposition_outcome.goal_extraction else "regex",
                "goal_count": len(
                    (decomposition_outcome.goal_extraction or goal_extraction).goals
                ),
            },
        )
        if decomposition_outcome.goal_extraction is not None:
            goal_extraction = decomposition_outcome.goal_extraction

        started, timer = trace_timer()
        report = self.graph_validator.validate(
            request,
            decomposition_outcome.result,
            effective,
            goal_extraction,
        )
        validation_status: Literal["success", "fallback"] = "success"
        validation_error: str | None = None
        if not report.valid:
            fallback_decomposer = (
                self.decomposers["deterministic"] if goal_extraction.decision == "multi" else self.decomposers["none"]
            )
            fallback = await fallback_decomposer.decompose(
                request,
                understanding.analysis,
                profile,
                effective,
                f"Invalid generated graph; fallback applied: {report.errors}",
                goal_extraction,
            )
            fallback.result.validation_status = GraphValidationStatus.FALLBACK
            decomposition_outcome = fallback
            second_report = self.graph_validator.validate(
                request,
                decomposition_outcome.result,
                effective,
                goal_extraction,
            )
            validation_status = "fallback"
            validation_error = "; ".join(report.errors)
            report = second_report
        traces.add(
            stage="decomposition_validation",
            strategy="deterministic-dag-validator",
            started_at=started,
            timer_started=timer,
            status=validation_status,
            error=validation_error,
            metadata={"valid": report.valid, "errors": report.errors, "warnings": report.warnings},
        )

        subtask_signals: dict[str, RoutingSignals] | None = None
        if self.config.decomposition_mode == "difficulty_gap" and len(decomposition_outcome.result.subtasks) > 1:
            subtask_signals = await self._score_subtasks(decomposition_outcome.result, effective, traces)
            if subtask_signals is None:
                decomposition_outcome = await self.decomposers["none"].decompose(
                    request,
                    understanding.analysis,
                    profile,
                    effective,
                    f"{decomposition_reason}; subtask tiers do not differ enough, folded into one task",
                    goal_extraction,
                )
                profile.decomposition_recommended = False

        decomposition_outcome.result = self._attach_routing_signals(
            decomposition_outcome.result, understanding.analysis, attachments, request, subtask_signals
        )
        result = PipelineResult(
            request=request,
            validation=validation,
            goal_extraction=goal_extraction,
            effective_constraints=effective,
            semantic_analysis=understanding.analysis,
            attachments=attachments,
            profile=profile,
            decomposition=decomposition_outcome.result,
            traces=traces.traces,
            costs=summarize_costs(traces.traces),
        )
        self._cache_put(cache_key, result)
        return result

    # A subtask's own difficulty caps the request-level reasoning score, so
    # "translate X and prove Y" does not send the translation to the
    # reasoning tier just because the request as a whole scored high.
    _REASONING_CAP = {Difficulty.EASY: 1.0, Difficulty.MEDIUM: 2.0, Difficulty.HARD: 3.0}

    def _split_gate_applies(self, request: GatewayRequest) -> bool:
        return (
            self.config.decomposition_mode == "difficulty_gap"
            and self.split_gate is not None
            and not request.metadata.get("disable_decomposition")
        )

    async def _timed_split_gate(self, routing_text: str) -> tuple[SplitGateOutcome, datetime, float]:
        assert self.split_gate is not None
        started, timer = trace_timer()
        outcome = await self.split_gate.check(routing_text)
        return outcome, started, perf_counter() - timer

    def _trace_split_gate(
        self, outcome: SplitGateOutcome, started: datetime, elapsed: float, traces: TraceCollector
    ) -> float | None:
        cost, estimated = self.pricing.calculate_usage(
            outcome.model or "offline", outcome.usage.input_tokens, outcome.usage.output_tokens,
            getattr(outcome, "output_evidence", None),
        )
        traces.add(
            stage="decompose_gate",
            strategy="jev-split-gate",
            model=outcome.model,
            started_at=started,
            # Ran next to the understand call: report its own duration, not the wait for both.
            timer_started=perf_counter() - elapsed,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            cost_usd=cost,
            status="success" if outcome.gap_probability is not None else "fallback",
            error=outcome.error,
            metadata={"cost_estimated": estimated, "gap_probability": outcome.gap_probability},
        )
        return outcome.gap_probability

    async def _score_subtasks(
        self, decomposition: DecompositionResult, effective: EffectiveConstraints, traces: TraceCollector
    ) -> dict[str, RoutingSignals] | None:
        """Scores each subtask with jev and keeps the split only when the easiest and the hardest
        subtask are ``split_min_tier_gap`` tier steps apart. Returns each subtask's own signals,
        or None to fold the request back into one task (also when any subtask cannot be scored)."""
        started, timer = trace_timer()
        scorer = self.understanders.get("jev")
        subtasks = decomposition.subtasks
        scores = []
        if not isinstance(scorer, JevUnderstander) or not scorer.available:
            error: str | None = "jev is not available to score subtasks"
        else:
            scores = await asyncio.gather(
                *(
                    scorer.score_text(f"{task.description}\n\nExpected output: {task.expected_output}")
                    for task in subtasks
                )
            )
            failed = [score.error or "no signals" for score in scores if score.signals is None]
            error = "; ".join(failed) or None
        input_tokens = sum(score.usage.input_tokens for score in scores)
        output_tokens = sum(score.usage.output_tokens for score in scores)
        cost, estimated = (
            self.pricing.calculate(JevUnderstander.model_id, input_tokens, output_tokens) if scores else (0.0, False)
        )
        tiers: dict[str, str] = {}
        if error is None:
            for task, score in zip(subtasks, scores, strict=True):
                tiers[task.task_id] = self.tier_policy.decide(
                    score.signals,
                    task_type=score.task_type or task.task_type,
                    quality_priority=effective.constraints.quality_priority,
                ).tier
        steps = [TIER_ORDER.index(tier) for tier in tiers.values()]
        gap = max(steps) - min(steps) if steps else 0
        kept = error is None and gap >= self.config.split_min_tier_gap
        traces.add(
            stage="decompose_scoring",
            strategy="jev-per-subtask",
            model=JevUnderstander.model_id if scores else None,
            started_at=started,
            timer_started=timer,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            status="success" if error is None else "fallback",
            error=error,
            metadata={
                "cost_estimated": estimated,
                "tiers": tiers,
                "tier_gap": gap,
                "min_tier_gap": self.config.split_min_tier_gap,
                "split_kept": kept,
            },
        )
        if not kept:
            return None
        return {task.task_id: score.signals for task, score in zip(subtasks, scores, strict=True)}

    @classmethod
    def _attach_routing_signals(
        cls,
        decomposition: DecompositionResult,
        analysis: SemanticAnalysis,
        attachments: list[AttachmentAnalysis] | None = None,
        request: GatewayRequest | None = None,
        subtask_signals: dict[str, RoutingSignals] | None = None,
    ) -> DecompositionResult:
        signals = analysis.signals()
        # Reading load of the latest request (earlier turns excluded, so a
        # short follow-up in a long chat stays cheap): drives the long-input floor.
        input_chars = (len(request.focus_text()) if request is not None else 0) + sum(
            len(item.extracted_text or "") for item in attachments or []
        )
        # What the executing model will actually be sent (the same helpers and caps the
        # orchestrator uses): decides which models' context windows are large enough.
        context_chars = (
            len(request.conversation_text()) + len(request.focus_text()) if request is not None else 0
        ) + len(execution_context(attachments or []))
        signals = signals.model_copy(update={"input_chars": input_chars, "context_chars": context_chars})
        if attachments:
            # Every subtask gets the request's attachment floors for now;
            # per-subtask attachment references would narrow this.
            visual, unreadable = attachment_routing_counts(attachments)
            signals = signals.model_copy(update={"visual_attachments": visual, "unreadable_attachments": unreadable})
        several = len(decomposition.subtasks) > 1
        subtasks = []
        request_fields = ("input_chars", "context_chars", "visual_attachments", "unreadable_attachments")
        for task in decomposition.subtasks:
            own = (subtask_signals or {}).get(task.task_id)
            if task.routing_signals is None and own is not None:
                # Scored on its own by jev: its tier is already its own, no request-level cap.
                own = own.model_copy(update={key: getattr(signals, key) for key in request_fields})
                task = task.model_copy(update={"routing_signals": own})
            elif task.routing_signals is None:
                task_signals = signals
                cap = cls._REASONING_CAP[task.difficulty]
                if several and signals.reasoning > cap:
                    task_signals = signals.model_copy(update={"reasoning": cap})
                task = task.model_copy(update={"routing_signals": task_signals})
            subtasks.append(task)
        return decomposition.model_copy(update={"subtasks": subtasks})

    async def _analyze_attachments(
        self, request: GatewayRequest, effective: EffectiveConstraints, traces: TraceCollector
    ) -> list[AttachmentAnalysis]:
        if not request.has_attachments():
            return []
        started, timer = trace_timer()
        analyses = await self.attachment_analyzer.analyze(request, effective.constraints)
        cost = 0.0
        cost_estimated = False
        for item in analyses:
            item_cost, item_estimated = self.pricing.calculate_usage(
                item.model or "offline", item.usage.input_tokens, item.usage.output_tokens,
                getattr(item, "output_evidence", None),
            )
            cost += item_cost
            cost_estimated = cost_estimated or item_estimated
        models = sorted({item.model for item in analyses if item.model})
        unanalyzed = [item for item in analyses if item.status != "analyzed"]
        traces.add(
            stage="attachment_analysis",
            strategy="+".join(sorted({item.method for item in analyses})),
            model="+".join(models) or None,
            started_at=started,
            timer_started=timer,
            input_tokens=sum(item.usage.input_tokens for item in analyses),
            output_tokens=sum(item.usage.output_tokens for item in analyses),
            cost_usd=cost,
            status="fallback" if unanalyzed else "success",
            error="; ".join(f"{item.attachment_id}: {item.error}" for item in unanalyzed if item.error) or None,
            metadata={
                "cost_estimated": cost_estimated,
                "attachment_count": len(analyses),
                "usage_complete":all(item.usage_complete for item in analyses if item.model),
                "analyzed_count": len(analyses) - len(unanalyzed),
                "cache_hits": sum(item.cache_hit for item in analyses),
                "attachments": [
                    {
                        "attachment_id": item.attachment_id,
                        "kind": item.kind.value,
                        "status": item.status,
                        "method": item.method,
                        "content_category": item.content_category,
                        "extracted_chars": len(item.extracted_text),
                    }
                    for item in analyses
                ],
            },
        )
        request.set_attachment_digest(routing_digest(analyses))
        return analyses

    @staticmethod
    def _with_attachment_signals(analysis, attachments: list[AttachmentAnalysis]):
        updated = analysis.model_copy(deep=True)
        for capability in required_capabilities(attachments):
            if capability not in updated.required_capabilities:
                updated.required_capabilities.append(capability)
        summary = ", ".join(
            f"{item.attachment_id}={item.kind.value}/{item.content_category or item.status}" for item in attachments
        )
        updated.explanations.append(f"Attachments considered via text surrogate: {summary}.")
        return updated

    async def _shadow_classify(self, text: str) -> dict[str, object]:
        try:
            return (await self.task_classifier_shadow.aclassify(text)).as_metadata()
        except Exception as exc:  # shadow output must never fail the request
            return {"error": str(exc)[:200]}

    def _cache_key(self, request: GatewayRequest, understander: str, decomposer: str) -> str:
        application_policy = self.application_policies.get(request.application_id)
        payload = {
            "pipeline_version": self.config.pipeline_version,
            "config": self.config.model_dump(mode="json"),
            "original_prompt": request.prompt_text(),
            "normalized_prompt": normalize_prompt(request.prompt_text()),
            "prompt": request.prompt,
            "messages": [message.model_dump(mode="json") for message in request.messages or []],
            "application_id": request.application_id,
            "context": request.context,
            "constraints": request.constraints.model_dump(mode="json"),
            "metadata": request.metadata,
            "platform_policy": self.platform_policy.model_dump(mode="json") if self.platform_policy else None,
            "application_policy": application_policy.model_dump(mode="json") if application_policy else None,
            "understander": understander,
            "decomposer": decomposer,
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str, request: GatewayRequest) -> PipelineResult | None:
        cached = self._result_cache.get(key)
        if cached is None:
            return None
        self._result_cache.move_to_end(key)
        result = cached.model_copy(deep=True)
        result.request = request
        if result.decomposition is not None:
            result.decomposition.original_request_id = request.request_id
        for trace in result.traces:
            trace.request_id = request.request_id
            trace.input_tokens = 0
            trace.output_tokens = 0
            trace.cost_usd = 0
            trace.latency_ms = 0
            trace.metadata["cache_hit"] = True
        result.costs = CostSummary()
        return result

    def _cache_put(self, key: str, result: PipelineResult) -> None:
        if self.config.pipeline_cache_max_entries <= 0:
            return
        self._result_cache[key] = result.model_copy(deep=True)
        self._result_cache.move_to_end(key)
        while len(self._result_cache) > self.config.pipeline_cache_max_entries:
            self._result_cache.popitem(last=False)

    @staticmethod
    def _append_skipped(traces: TraceCollector, reason: str, after: str | None = None) -> None:
        stages = [
            "goal_extraction",
            "constraint_resolution",
            "understand",
            "request_scoring",
            "decomposition_decision",
            "decompose",
            "decomposition_validation",
        ]
        if after in stages:
            stages = stages[stages.index(after) + 1 :]
        for stage in stages:
            started, timer = trace_timer()
            traces.add(
                stage=stage,
                strategy="not-run",
                started_at=started,
                timer_started=timer,
                status="skipped",
                error=reason,
            )
