"""Routes decomposed subtasks to a model using jev, via LangChain's own
harness for it rather than a hand-rolled classifier call, and executes them
with a fixed-model fallback on transient failure.

Reference: https://www.langchain.com/blog/building-a-harness-with-jev

Route + execute are one step, not two
--------------------------------------
- `ModelRouterMiddleware` reads the latest user message and has jev pick the
  `ModelChoice` whose `criteria` best matches it, then immediately calls
  that model — both happen inside a single `agent.invoke()`.
- `AutoModeMiddleware` has jev screen tool calls the subtask triggers and
  blocks risky ones before they execute (optional, via `risky_tools`).

There's no decide-only / dry-run mode in the SDK, so a separate
Router -> RouteDecision -> Executor split (Executor holding its own
ModelRegistry/LLMClient abstraction) would mean dropping ModelRouterMiddleware
and hand-rolling model selection again. Instead, fallback is a thin layer
*around* `agent.invoke()`: on a transient error (timeout/rate-limit/5xx/
connection), retry once against a fixed model (`ExecutionConfig.fallback_model`)
that bypasses jev entirely — jev is not asked to pick a different model, since
if jev/the provider is already degraded, asking it again is the same failure
mode. Permanent errors (auth, bad request, anything unclassified) fail
immediately — a fallback would not help and could mask a bug.

Takes `SubTask` straight from the gateway's decomposition output
(`src.shared.dtos.gateway.SubTask`) instead of a routing-local schema, so no
adapter is needed between decompose and routing. Wire it up as:

    result: DecompositionResult = await pipeline.run(request, ...)  # gateway
    results = await SubtaskRouter().aexecute_subtasks(
        result.decomposition.subtasks,
        original_prompt=request.prompt_text(),
        request_context=request.context,
    )  # this module
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from src.shared.config import Settings, get_settings
from src.shared.core.pricing import ModelPricing, PricingRegistry
from src.shared.dtos.gateway import OutputEvidence, SubTask

from .errors import TransientExecutionError, classify_error

# Reference OpenAI per-million-token pricing for the models this router can
# select (small_fast/mid/large_reasoning tiers + fallback). Not sourced from
# Settings because there's no per-tier pricing config yet — see
# ExecutionConfig.from_settings. Kept local to this module and marked
# `estimated=True` since real prices drift over time; an unrecognized model
# (a tier reconfigured to something not in this table) falls back to
# PricingRegistry's own 0-cost/estimated=True behavior rather than crashing.
_DEFAULT_MODEL_PRICING = [
    ModelPricing(
        model_id="gpt-4o-mini",
        provider="openai",
        input_price_per_million=0.15,
        output_price_per_million=0.60,
        pricing_version="openai-docs-2026-09-21",
        estimated=True,
    ),
    ModelPricing(
        model_id="gpt-4o",
        provider="openai",
        input_price_per_million=2.50,
        output_price_per_million=10.00,
        pricing_version="openai-docs-2026-09-21",
        estimated=True,
    ),
    ModelPricing(
        model_id="o1",
        provider="openai",
        input_price_per_million=15.00,
        output_price_per_million=60.00,
        pricing_version="openai-docs-2026-09-21",
        estimated=True,
    ),
]


def _bare_model_name(name: str) -> str:
    """Strips SubtaskRouter's "openai:<model>" display prefix so it matches
    PricingRegistry's bare model_id keys."""
    return name.split(":", 1)[-1] if ":" in name else name


def _usage_from_message(message: object) -> tuple[int, int]:
    """Reads (input_tokens, output_tokens) off an AIMessage's usage_metadata.

    Returns (0, 0) for fakes/messages without it (e.g. test doubles) instead
    of raising, since usage accounting is best-effort, not load-bearing for
    routing/execution itself.
    """
    usage = getattr(message, "usage_metadata", None) or {}
    return usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def _output_evidence_from_message(message: object) -> OutputEvidence:
    metadata = getattr(message, "response_metadata", None) or {}
    usage = getattr(message, "usage_metadata", None) or {}
    token_usage = metadata.get("token_usage", {}) if isinstance(metadata, dict) else {}
    input_details = usage.get("input_token_details", {}) if isinstance(usage, dict) else {}
    output_details = usage.get("output_token_details", {}) if isinstance(usage, dict) else {}
    prompt_details = token_usage.get("prompt_tokens_details", {}) if isinstance(token_usage, dict) else {}
    completion_details = token_usage.get("completion_tokens_details", {}) if isinstance(token_usage, dict) else {}
    reason = metadata.get("finish_reason") or metadata.get("stop_reason") if isinstance(metadata, dict) else None
    reason = {
        "stop": "stop", "end_turn": "stop", "length": "length", "max_tokens": "length",
        "content_filter": "content_filter", "safety": "content_filter",
    }.get(str(reason).lower(), "unknown")

    def counter(*values):
        return next((value for value in values if type(value) is int and value >= 0), None)

    cache_creation = (
        input_details.get("cache_creation", {})
        if isinstance(input_details, dict)
        else prompt_details.get("cache_creation", {})
    )
    if not isinstance(cache_creation, dict):
        cache_creation = {}
    return OutputEvidence(
        finish_reason=reason,
        cached_tokens=counter(prompt_details.get("cached_tokens"), input_details.get("cache_read")),
        cache_write_tokens=counter(prompt_details.get("cache_write_tokens"), input_details.get("cache_creation")),
        cache_write_5m_tokens=counter(cache_creation.get("ephemeral_5m_input_tokens"),
            prompt_details.get("cache_creation_5m_input_tokens")),
        cache_write_1h_tokens=counter(cache_creation.get("ephemeral_1h_input_tokens"),
            prompt_details.get("cache_creation_1h_input_tokens")),
        reasoning_tokens=counter(completion_details.get("reasoning_tokens"), output_details.get("reasoning")),
    )


@dataclass
class RoutingLogEntry:
    task_id: str
    selected_model: str
    route: str
    confidence: float
    response: str

    def __str__(self) -> str:
        return f"{self.task_id}\n  selected_model: {self.selected_model}\n  response: {self.response}"


@dataclass
class ExecutionConfig:
    fallback_model: str = ""
    timeout_seconds: float = 30.0
    retry_limit: int = 0
    max_output_tokens: int | None = None
    hard_completion_token_limit: int | None = None
    deadline_at: float | None = None
    generation_options_enabled: bool = False
    stream_output: bool = False
    stream_task_ids: tuple[str, ...] = ()
    shared_completion_budget: bool = False

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ExecutionConfig:
        settings = settings or get_settings()
        return cls(
            fallback_model=settings.gateway_routing_fallback_model,
            timeout_seconds=settings.gateway_routing_timeout_seconds,
        )


@dataclass
class ExecutionResult:
    task_id: str
    status: Literal["success", "failed"]
    result: str | None
    selected_model: str | None
    """The model jev routed to. None if jev/the primary call never returned
    a routing decision (error happened before or during classification)."""
    executed_model: str | None
    """The model that actually produced `result` — equals `selected_model`
    on a normal run, or the fallback model when `fallback=True`."""
    fallback: bool
    route: str | None = None
    """The jev routing tier (small_fast/mid/large_reasoning). It is None
    when routing did not complete or execution used the fixed fallback."""
    failure_type: str | None = None
    failure_reason: str | None = None
    latency_ms: float = 0.0
    confidence: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_estimated: bool = False
    provider: str | None = None
    usage_complete: bool = False
    final_input_tokens: int | None = None
    final_output_tokens: int | None = None
    final_usage_complete: bool | None = None
    circuit_skipped: list[str] = field(default_factory=list)
    route_reason: str | None = None
    """Why the router picked this tier/model (TierPolicy rule and inputs)."""
    route_shadow: dict | None = None
    """Shadow tier decision for comparison logs (did not affect routing)."""
    """Candidates skipped because their circuit breaker was open."""
    output_evidence: OutputEvidence = field(default_factory=OutputEvidence)

    def __str__(self) -> str:
        return (
            f"{self.task_id}\n"
            f"  status: {self.status}\n"
            f"  selected_model: {self.selected_model}\n"
            f"  executed_model: {self.executed_model}\n"
            f"  fallback: {self.fallback}"
            + (f"\n  failure: {self.failure_type}: {self.failure_reason}" if self.failure_type else "")
            + (f"\n  result: {self.result}" if self.result else "")
        )


def _build_model_choices(
    timeout_seconds: float,
    *,
    settings: Settings | None = None,
    api_key: str,
    base_url: str | None = None,
) -> tuple[dict[str, object], dict[str, str]]:
    from langchain_openai import ChatOpenAI
    from langchain_typesafe.experimental.middleware import ModelChoice

    """Returns (choices for ModelRouterMiddleware, tier -> display name).

    ModelChoice.model takes a constructed BaseChatModel (not just a model
    string) so a timeout can be enforced — that's also what turns a hung
    call into a classified TransientExecutionError instead of blocking
    forever. Display names are tracked separately because logging a
    ChatOpenAI repr as "the selected model" would be unreadable.
    """
    settings = settings or get_settings()
    tiers = {
        "small_fast": (
            settings.gateway_model_tier_small_fast,
            "Direct lookups, formatting, short extraction or single-step analysis.",
        ),
        "mid": (settings.gateway_model_tier_mid, "Standard coding or multi-step analysis tasks."),
        "large_reasoning": (
            settings.gateway_model_tier_large_reasoning,
            "Architecture-level, ambiguous, or high-stakes tasks.",
        ),
    }
    choices: dict[str, ModelChoice] = {}
    names: dict[str, str] = {}
    for tier, (model_name, criteria) in tiers.items():
        choices[tier] = ModelChoice(
            model=ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
                max_retries=0,
            ),
            criteria=criteria,
        )
        names[tier] = f"openai:{model_name}"
    return choices, names


_TYPESAFE_ENV_LOCK = threading.RLock()


@contextmanager
def _temporary_typesafe_api_key(api_key: str):
    with _TYPESAFE_ENV_LOCK:
        previous = os.environ.get("TYPESAFE_API_KEY")
        try:
            os.environ["TYPESAFE_API_KEY"] = api_key
            yield
        finally:
            if previous is None:
                os.environ.pop("TYPESAFE_API_KEY", None)
            else:
                os.environ["TYPESAFE_API_KEY"] = previous


class SubtaskRouter:
    """One routing-aware agent shared across subtasks. jev picks the model
    per subtask instruction at invoke time (ModelRouterMiddleware) and
    screens any tool call the subtask triggers (AutoModeMiddleware)."""

    def __init__(
        self,
        base_model: str | None = None,
        tools: list | None = None,
        risky_tools: list[str] | None = None,
        execution_config: ExecutionConfig | None = None,
        *,
        agent: object | None = None,
        fallback_model: object | None = None,
        model_names: dict[str, str] | None = None,
        pricing: PricingRegistry | None = None,
        settings: Settings | None = None,
        openai_api_key: str | None = None,
        openai_base_url: str | None = None,
        typesafe_api_key: str | None = None,
    ) -> None:
        settings = settings or get_settings()
        self._execution_config = execution_config or ExecutionConfig.from_settings(settings)
        self._pricing = pricing or PricingRegistry(_DEFAULT_MODEL_PRICING)
        self._model_names = model_names or {}

        if agent is None:
            from langchain.agents import create_agent
            from langchain_typesafe.experimental.middleware import (
                AutoModeMiddleware,
                ModelRouterMiddleware,
            )

            if not openai_api_key or not typesafe_api_key:
                raise ValueError("Internal OpenAI and JEV credentials are required for the legacy JEV router.")
            self._model_choices, self._model_names = _build_model_choices(
                self._execution_config.timeout_seconds,
                settings=settings,
                api_key=openai_api_key,
                base_url=openai_base_url,
            )
            # ModelRouterMiddleware constructs its default TypeSafeClassifier
            # inside __init__, before the classifier attribute can be replaced.
            # Serialize the temporary process-environment override and restore it
            # even when the SDK constructor raises.
            with _temporary_typesafe_api_key(typesafe_api_key):
                model_router_middleware = ModelRouterMiddleware(
                    choices=self._model_choices,
                    instructions="Choose the least costly model that can complete the task.",
                )
            from langchain_typesafe import TypeSafeClassifier

            model_router_middleware.classifier = TypeSafeClassifier(api_key=typesafe_api_key)
            middleware = [model_router_middleware]
            if risky_tools:
                middleware.append(AutoModeMiddleware(tools=risky_tools))
            agent_model = base_model
            if agent_model is None:
                from langchain_openai import ChatOpenAI

                agent_model = ChatOpenAI(
                    model=settings.gateway_model_tier_mid,
                    api_key=openai_api_key,
                    base_url=openai_base_url,
                    timeout=self._execution_config.timeout_seconds,
                )
            self._agent = create_agent(
                agent_model,
                tools=tools or [],
                middleware=middleware,
            )
        else:
            self._agent = agent

        if fallback_model is not None:
            self._fallback_model = fallback_model
        else:
            from langchain_openai import ChatOpenAI

            self._fallback_model = ChatOpenAI(
                model=self._execution_config.fallback_model,
                api_key=openai_api_key,
                base_url=openai_base_url,
                timeout=self._execution_config.timeout_seconds,
            )

    @staticmethod
    def _build_message(
        subtask: SubTask,
        upstream_results: dict[str, str] | None = None,
        *,
        original_prompt: str | None = None,
        request_context: dict[str, object] | None = None,
    ) -> str:
        # Preserve the compact message for direct router callers that do not
        # have request-level context. GatewayOrchestrator (via SubtaskExecutor
        # /JevRouter) always supplies the original prompt, so UI/E2E execution
        # takes the context-rich path.
        if not original_prompt and not request_context and not upstream_results:
            message = subtask.description
            if subtask.required_context:
                message += "\n\nContext: " + "; ".join(subtask.required_context)
            if subtask.required_capabilities:
                message += "\n\nRequired capabilities: " + ", ".join(subtask.required_capabilities)
            return message

        sections = [
            (
                "You are executing exactly one subtask from a larger request. "
                "Complete only the subtask instruction below. Use the original "
                "request as source material, not as a request to redo every task."
            ),
            f"Subtask instruction:\n{subtask.description}",
            f"Expected output:\n{subtask.expected_output}",
        ]
        if subtask.constraints:
            sections.append(
                "Subtask constraints:\n" + json.dumps(subtask.constraints, ensure_ascii=False, default=str, indent=2)
            )
        if subtask.required_context:
            sections.append("Required context references:\n" + "; ".join(subtask.required_context))
        if subtask.required_capabilities:
            sections.append("Required capabilities:\n" + ", ".join(subtask.required_capabilities))
        if upstream_results:
            input_from = subtask.input_from or {}
            dependency_sections = []
            for dep_id, text in upstream_results.items():
                label = f"{input_from[dep_id]} from {dep_id}" if dep_id in input_from else f"Output from {dep_id}"
                dependency_sections.append(f"{label}:\n{text}")
            sections.append("Dependency outputs (authoritative inputs):\n" + "\n\n".join(dependency_sections))
        if original_prompt:
            sections.append(f"Original user request and source material:\n{original_prompt}")
        if request_context:
            sections.append(
                "Structured request context:\n" + json.dumps(request_context, ensure_ascii=False, default=str, indent=2)
            )
        return "\n\n".join(sections)

    # -- route + execute in one shot, no fallback (kept exactly as first
    # verified working end-to-end; do not fold fallback logic in here) --

    def route_subtask(self, subtask: SubTask) -> dict:
        message = self._build_message(subtask)
        return self._agent.invoke({"messages": [{"role": "user", "content": message}]})

    async def aroute_subtask(self, subtask: SubTask) -> dict:
        message = self._build_message(subtask)
        return await self._agent.ainvoke({"messages": [{"role": "user", "content": message}]})

    def route_subtasks(self, subtasks: list[SubTask]) -> list[dict]:
        """Runs subtasks in dependency order (topological, one wave at a
        time) so a task never runs before the subtasks it depends on."""
        done: set[str] = set()
        results: dict[str, dict] = {}
        remaining = list(subtasks)

        while remaining:
            ready = [t for t in remaining if all(d in done for d in t.dependencies)]
            if not ready:
                raise ValueError(f"Cyclic or unresolved dependencies among subtasks: {[t.task_id for t in remaining]}")
            for t in ready:
                results[t.task_id] = self.route_subtask(t)
                done.add(t.task_id)
            remaining = [t for t in remaining if t.task_id not in done]

        return [results[t.task_id] for t in subtasks]

    def _to_log_entry(self, subtask: SubTask, result: dict) -> RoutingLogEntry:
        """Reads the `model_route` jev left in agent state (see
        ModelRouterMiddleware.before_agent) — doesn't call jev again."""
        route_answer = result["model_route"]
        chosen = self._model_names[route_answer.choice]
        last_message = result["messages"][-1]
        response = getattr(last_message, "content", str(last_message))
        return RoutingLogEntry(
            task_id=subtask.task_id,
            selected_model=chosen,
            route=route_answer.choice,
            confidence=route_answer.confidence,
            response=response,
        )

    def route_subtasks_with_log(self, subtasks: list[SubTask]) -> list[RoutingLogEntry]:
        """Same routing as `route_subtasks`, reshaped into `task_id /
        selected_model / response` entries for logging/printing."""
        results = self.route_subtasks(subtasks)
        return [self._to_log_entry(subtask, result) for subtask, result in zip(subtasks, results)]

    # -- execute: route_subtask wrapped with error classification, fixed
    # fallback, and DAG-aware parallel scheduling with dependency handoff --

    async def _execute_fallback(
        self, subtask: SubTask, message: str, error: Exception, started_at: float
    ) -> ExecutionResult:
        fallback_name = f"openai:{self._execution_config.fallback_model}"
        try:
            response = await self._fallback_model.ainvoke(message)
            input_tokens, output_tokens = _usage_from_message(response)
            evidence = _output_evidence_from_message(response)
            cost_usd, cost_estimated = self._pricing.calculate_usage(
                self._execution_config.fallback_model, input_tokens, output_tokens, evidence
            )
            return ExecutionResult(
                task_id=subtask.task_id,
                status="success",
                result=response.content,
                selected_model=None,
                executed_model=fallback_name,
                fallback=True,
                failure_type=type(error).__name__,
                failure_reason=str(error),
                latency_ms=(time.monotonic() - started_at) * 1000,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                cost_estimated=cost_estimated,
                output_evidence=evidence,
            )
        except Exception as fallback_error:
            return ExecutionResult(
                task_id=subtask.task_id,
                status="failed",
                result=None,
                selected_model=None,
                executed_model=fallback_name,
                fallback=True,
                failure_type=type(fallback_error).__name__,
                failure_reason=str(fallback_error),
                latency_ms=(time.monotonic() - started_at) * 1000,
            )

    async def aexecute_subtask(
        self,
        subtask: SubTask,
        upstream_results: dict[str, str] | None = None,
        *,
        original_prompt: str | None = None,
        request_context: dict[str, object] | None = None,
    ) -> ExecutionResult:
        message = self._build_message(
            subtask,
            upstream_results,
            original_prompt=original_prompt,
            request_context=request_context,
        )
        started_at = time.monotonic()
        try:
            raw = await self._agent.ainvoke({"messages": [{"role": "user", "content": message}]})
        except Exception as exc:
            if classify_error(exc) is TransientExecutionError:
                return await self._execute_fallback(subtask, message, exc, started_at)
            return ExecutionResult(
                task_id=subtask.task_id,
                status="failed",
                result=None,
                selected_model=None,
                executed_model=None,
                fallback=False,
                failure_type=type(exc).__name__,
                failure_reason=str(exc),
                latency_ms=(time.monotonic() - started_at) * 1000,
            )

        route_answer = raw["model_route"]
        model_name = self._model_names[route_answer.choice]
        last_message = raw["messages"][-1]
        input_tokens, output_tokens = _usage_from_message(last_message)
        evidence = _output_evidence_from_message(last_message)
        cost_usd, cost_estimated = self._pricing.calculate_usage(
            _bare_model_name(model_name), input_tokens, output_tokens, evidence
        )
        return ExecutionResult(
            task_id=subtask.task_id,
            status="success",
            result=getattr(last_message, "content", str(last_message)),
            selected_model=model_name,
            executed_model=model_name,
            fallback=False,
            route=route_answer.choice,
            latency_ms=(time.monotonic() - started_at) * 1000,
            confidence=route_answer.confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cost_estimated=cost_estimated,
            output_evidence=evidence,
        )

    def execute_subtask(
        self,
        subtask: SubTask,
        upstream_results: dict[str, str] | None = None,
        *,
        original_prompt: str | None = None,
        request_context: dict[str, object] | None = None,
    ) -> ExecutionResult:
        return asyncio.run(
            self.aexecute_subtask(
                subtask,
                upstream_results,
                original_prompt=original_prompt,
                request_context=request_context,
            )
        )

    @staticmethod
    def _collect_upstream_results(subtask: SubTask, done: dict[str, ExecutionResult]) -> dict[str, str]:
        return {
            dep_id: done[dep_id].result for dep_id in subtask.dependencies if dep_id in done and done[dep_id].result
        }

    async def aexecute_subtasks(
        self,
        subtasks: list[SubTask],
        *,
        original_prompt: str | None = None,
        request_context: dict[str, object] | None = None,
    ) -> list[ExecutionResult]:
        """Runs subtasks in dependency order, in parallel within each wave
        (asyncio.gather), passing each dependency's result — filtered
        through `input_from` when set — into the dependent task's message.
        A task whose dependency failed is not attempted; it's marked failed
        with `failure_type="UpstreamFailure"` so the trace stays honest
        instead of running on missing context."""
        done: dict[str, ExecutionResult] = {}
        remaining = list(subtasks)

        while remaining:
            ready = [t for t in remaining if all(d in done for d in t.dependencies)]
            if not ready:
                raise ValueError(f"Cyclic or unresolved dependencies among subtasks: {[t.task_id for t in remaining]}")

            runnable = [t for t in ready if not any(done[d].status == "failed" for d in t.dependencies)]
            blocked = [t for t in ready if t not in runnable]

            wave = await asyncio.gather(
                *(
                    self.aexecute_subtask(
                        t,
                        self._collect_upstream_results(t, done),
                        original_prompt=original_prompt,
                        request_context=request_context,
                    )
                    for t in runnable
                )
            )
            for t, result in zip(runnable, wave):
                done[t.task_id] = result
            for t in blocked:
                failed_deps = [d for d in t.dependencies if done[d].status == "failed"]
                done[t.task_id] = ExecutionResult(
                    task_id=t.task_id,
                    status="failed",
                    result=None,
                    selected_model=None,
                    executed_model=None,
                    fallback=False,
                    failure_type="UpstreamFailure",
                    failure_reason=f"Dependencies failed: {failed_deps}",
                )

            remaining = [t for t in remaining if t.task_id not in done]

        return [done[t.task_id] for t in subtasks]

    def execute_subtasks(
        self,
        subtasks: list[SubTask],
        *,
        original_prompt: str | None = None,
        request_context: dict[str, object] | None = None,
    ) -> list[ExecutionResult]:
        return asyncio.run(
            self.aexecute_subtasks(
                subtasks,
                original_prompt=original_prompt,
                request_context=request_context,
            )
        )


def execution_trace_rows(subtasks: list[SubTask], results: list[ExecutionResult]) -> list[dict]:
    """Reshapes (SubTask, ExecutionResult) pairs into the same
    stage/strategy-model/tokens/cost/latency/status/error row shape the
    gateway pipeline's Trace tab uses, so route+execute can be monitored the
    same way. One row per subtask: jev's routing decision and the routed
    model call happen inside a single agent.ainvoke() (see module docstring),
    so they are not separable into two rows — "route_execute" names that.
    """
    rows = []
    for subtask, result in zip(subtasks, results):
        strategy = "fixed-fallback" if result.fallback else "jev-model-router"
        model = result.executed_model or result.selected_model or "-"
        error = f"{result.failure_type}: {result.failure_reason}" if result.failure_type else None
        rows.append(
            {
                "stage": f"route_execute:{subtask.task_id}",
                "strategy/model": f"{strategy} / {model}",
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "latency_ms": round(result.latency_ms, 3),
                "status": result.status,
                "error": error,
                "usage_complete": result.usage_complete,
                "route": result.route,
                "route_reason": result.route_reason,
                "route_shadow": result.route_shadow,
                "circuit_skipped": result.circuit_skipped,
                "output_evidence": result.output_evidence.model_dump(mode="json"),
            }
        )
    return rows
