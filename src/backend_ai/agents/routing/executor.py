from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from src.backend_ai.agents.routing.base import BaseRouter
from src.backend_ai.agents.routing.errors import TransientExecutionError, classify_error
from src.backend_ai.agents.routing.router import ExecutionConfig, ExecutionResult
from src.backend_ai.gateway.core.circuit_breaker import CircuitBreaker, retry_after_seconds
from src.backend_ai.gateway.providers.base import (
    GenerationOptions,
    ModelUnavailableError,
    ProviderClient,
    ProviderResponse,
    ProviderStreamEvent,
)
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.backend_ai.latency.context import current_latency_context
from src.backend_ai.latency.features import build_candidate_features
from src.shared.core.pricing import PricingRegistry
from src.shared.dtos.gateway import SubTask


@dataclass(frozen=True)
class ExecutionContext:
    """Request-level material every subtask of one request shares."""

    original_prompt: str | None = None
    """The routing focus (latest user ask)."""
    request_context: dict[str, object] | None = None
    conversation: str | None = None
    """System instructions + earlier turns; the subtask alone lacks them."""
    attachments: str | None = None
    """Text extracted from images/files (see gateway.multimodal.digest)."""
    source_material: str | None = None
    """Passage/data the request's tasks operate on (GoalExtractionResult.source_material).
    Shared with every subtask; the rest of the original prompt stays out so a
    subtask does not redo sibling tasks."""
    route_hints: dict[str, object] = field(default_factory=dict)
    """Soft routing preferences (e.g. quality_priority) for routers."""

    def router_prompt(self) -> str | None:
        """Route+execute routers (jev) build their own message from the
        original prompt, so conversation/attachment text rides along there."""
        parts = [part for part in (self.conversation, self.original_prompt, self.attachments) if part]
        return "\n\n".join(parts) if parts else None


class SubtaskExecutor:
    def __init__(
        self,
        router: BaseRouter,
        providers: dict[str, ProviderClient] | None = None,
        *,
        execution_config: ExecutionConfig | None = None,
        timeout_seconds: float | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        pricing: PricingRegistry | None = None,
    ) -> None:
        self._router = router
        self._execution_config = execution_config or (
            ExecutionConfig(timeout_seconds=timeout_seconds)
            if timeout_seconds is not None
            else ExecutionConfig.from_settings()
        )
        self._providers = providers if providers is not None else {"offline": OfflineProvider()}
        self._breaker = circuit_breaker
        self._pricing = pricing

    @staticmethod
    def _build_message(
        subtask: SubTask,
        upstream_results: dict[str, str] | None = None,
        *,
        context: ExecutionContext | None = None,
    ) -> str:
        context = context or ExecutionContext()
        sections = [
            "Complete only the assigned task below. Do not solve unrelated tasks from the original request.",
        ]
        if context.conversation:
            sections.append(
                "Conversation so far (reference only; the assigned task is what to do now):\n" + context.conversation
            )
        sections += [
            f"Task {subtask.task_id}: {subtask.description}",
            f"Expected output: {subtask.expected_output}",
        ]
        if context.source_material:
            sections.append("Source material (shared input for this task):\n" + context.source_material)
        if context.request_context:
            sections.append(f"Context: {context.request_context}")
        upstream_results = upstream_results or {}
        if subtask.input_from:
            selected = {key: upstream_results[key] for key in subtask.input_from if key in upstream_results}
        else:
            selected = upstream_results
        if selected:
            sections.append(
                "Declared dependency outputs:\n" + "\n".join(f"{key}: {value}" for key, value in selected.items())
            )
        if context.attachments:
            sections.append(context.attachments)
        return "\n\n".join(sections)

    def _provider_for(
        self,
        model_id: str,
        provider_name: str | None,
        provider_kind: str | None = None,
    ) -> ProviderClient | None:
        provider = self._providers.get(model_id)
        if provider is not None:
            return provider
        if provider_name:
            provider = self._providers.get(provider_name)
            if provider is not None:
                return provider
        # A named local connection is a real server when it has a base URL; if its client is
        # missing, fail rather than answer from the offline stub.
        if provider_kind == "offline" or (provider_kind == "local" and not provider_name):
            provider = self._providers.get(provider_kind)
            if provider is not None:
                return provider
            return self._providers.get("offline")
        # A decision that names no provider at all (deterministic router in offline mode) still runs
        # offline; one that names a real connection or kind never silently degrades to offline.
        if not provider_name and not provider_kind:
            return self._providers.get("offline")
        return None

    async def aexecute_subtask(
        self,
        subtask: SubTask,
        upstream_results: dict[str, str] | None = None,
        *,
        original_prompt: str | None = None,
        request_context: dict[str, object] | None = None,
        constraints: object | None = None,
        context: ExecutionContext | None = None,
    ) -> ExecutionResult:
        context = context or ExecutionContext(original_prompt=original_prompt, request_context=request_context)
        if (
            self._execution_config.shared_completion_budget
            and subtask.task_id in self._execution_config.stream_task_ids
            and self._execution_config.hard_completion_token_limit == 0
        ):
            from src.shared.dtos.gateway import OutputEvidence

            return ExecutionResult(
                task_id=subtask.task_id,
                status="success",
                result="",
                selected_model=None,
                executed_model=None,
                fallback=False,
                usage_complete=True,
                output_evidence=OutputEvidence(finish_reason="length"),
            )
        started = time.monotonic()
        route_kwargs: dict[str, object] = {
            "original_prompt": context.router_prompt(),
            "request_context": context.request_context,
            "route_hints": context.route_hints,
        }
        message = self._build_message(subtask, upstream_results, context=context)
        latency_context = current_latency_context()
        if latency_context is not None and latency_context.mode not in {"off", "disabled"}:
            feature_options = None
            if self._execution_config.generation_options_enabled:
                feature_options = GenerationOptions(
                    max_output_tokens=self._execution_config.max_output_tokens,
                    hard_completion_token_limit=self._execution_config.hard_completion_token_limit,
                )
            task_type = getattr(subtask.task_type, "value", subtask.task_type)
            route_kwargs["latency_features"] = build_candidate_features(
                latency_context,
                subtask.task_id,
                material_chars=len(message),
                options=feature_options or GenerationOptions(),
                task_type=str(task_type),
                signals=subtask.routing_signals,
                now=datetime.now(UTC),
                protocol="native",
                streaming=(
                    self._execution_config.stream_output
                    and subtask.task_id in self._execution_config.stream_task_ids
                ),
                routing_objective=context.route_hints.get("quality_priority"),
            )
        if constraints is not None and getattr(self._router, "name", None) == "database":
            route_kwargs["constraints"] = constraints
        decision = await self._router.aroute(subtask, upstream_results, **route_kwargs)
        if decision.prefetched_result is not None:
            # Route+execute routers already called the model; record the
            # outcome so the breaker still learns about that model's health.
            self._record_prefetched(decision.prefetched_result)
            return decision.prefetched_result
        primary = decision.selected_model or decision.tier
        candidates = [primary, *[item for item in decision.fallback_models if item != primary]]
        last_error: Exception | None = None
        had_unreceipted_attempt = False
        circuit_skipped: list[str] = []
        attempted = False
        # Tokens and cost of attempts that returned no usable text: they were
        # billed by the provider, so they are carried into the final result.
        wasted_input_tokens = wasted_output_tokens = 0
        wasted_cost = 0.0
        wasted_cost_estimated = False
        for index, model_id in enumerate(candidates):
            provider_name = decision.provider_name if index == 0 else decision.fallback_provider_names.get(model_id)
            provider_kind = decision.provider_kind if index == 0 else decision.fallback_provider_kinds.get(model_id)
            provider_model = decision.provider_model if index == 0 else decision.fallback_provider_models.get(model_id)
            input_price = (
                decision.input_price_per_million
                if index == 0
                else decision.fallback_input_prices_per_million.get(model_id)
            )
            output_price = (
                decision.output_price_per_million
                if index == 0
                else decision.fallback_output_prices_per_million.get(model_id)
            )
            pricing_estimated = (
                decision.pricing_estimated if index == 0 else decision.fallback_pricing_estimated.get(model_id, True)
            )
            breaker_key = CircuitBreaker.key(provider_model or model_id, provider_name or provider_kind)
            if self._breaker is not None and not self._breaker.allow(breaker_key):
                circuit_skipped.append(model_id)
                continue
            provider = self._provider_for(model_id, provider_name, provider_kind)
            if provider is None:
                if self._breaker is not None:
                    self._breaker.release_probe(breaker_key)
                last_error = ValueError(f"No provider configured for model '{model_id}'")
                break
            attempted = True
            response = None
            deadline_hit = False
            permanent_failure = False
            for attempt in range(self._execution_config.retry_limit + 1):
                observed_response = None
                remaining = self._remaining_seconds()
                if remaining is not None and remaining <= 0:
                    last_error = TimeoutError("Gateway operation deadline exceeded")
                    deadline_hit = True
                    break
                timeout_seconds = self._execution_config.timeout_seconds
                if remaining is not None:
                    timeout_seconds = min(timeout_seconds, remaining)
                options = (
                    GenerationOptions(
                        max_output_tokens=self._execution_config.max_output_tokens,
                        hard_completion_token_limit=self._execution_config.hard_completion_token_limit,
                    )
                    if self._execution_config.max_output_tokens is not None
                    else None
                )
                try:
                    kwargs = {
                        "task_id": subtask.task_id,
                        "model_id": provider_model or model_id,
                        "timeout_seconds": timeout_seconds,
                    }
                    if options is not None:
                        kwargs["options"] = options
                    from src.backend_ai.services.runtime_stream_service import current_stream

                    stream = current_stream()
                    public_task = (
                        self._execution_config.stream_output
                        and subtask.task_id in self._execution_config.stream_task_ids
                    )
                    if stream is not None and public_task:
                        stream.model = provider_model or model_id
                        prefix = ""
                        if len(self._execution_config.stream_task_ids) > 1:
                            order = self._execution_config.stream_task_ids.index(subtask.task_id)
                            prefix = ("\n\n" if order else "") + f"## Task {order + 1}\n\n"
                        async for event in provider.stream(message, **kwargs):
                            if event.final is not None:
                                if event.final.usage_received:
                                    observed_response = event.final
                                cost, estimated = self._response_cost(
                                    event.final, model_id, input_price, output_price, pricing_estimated
                                )
                                stream.record_usage(
                                    event.final,
                                    task_id=subtask.task_id,
                                    model_id=model_id,
                                    cost=cost,
                                    estimated=estimated,
                                )
                            if event.type == "final":
                                response = event.final
                            elif event.type == "text_delta":
                                await stream.emit(
                                    ProviderStreamEvent(type="text_delta", text=prefix + (event.text or ""))
                                )
                                prefix = ""
                        if response is None:
                            raise RuntimeError("Provider stream ended without a final receipt")
                    else:
                        response = await provider.generate(message, **kwargs)
                except Exception as exc:
                    last_error = exc
                    had_unreceipted_attempt = True
                    # A received usage frame remains billable if the stream
                    # fails before its final output receipt. Do not overwrite
                    # it with a zero-counter failed execution result.
                    observed_response = getattr(exc, "response", None) or observed_response
                    if isinstance(observed_response, ProviderResponse):
                        observed_cost, observed_estimated = self._response_cost(
                            observed_response, model_id, input_price, output_price, pricing_estimated
                        )
                        wasted_input_tokens += observed_response.input_tokens
                        wasted_output_tokens += observed_response.output_tokens
                        wasted_cost += float(observed_cost)
                        wasted_cost_estimated = wasted_cost_estimated or observed_estimated
                    if stream is not None and stream.published:
                        permanent_failure = True
                        break
                    if isinstance(exc, ModelUnavailableError):
                        # This model cannot serve the request; the next one can.
                        break
                    if classify_error(exc).__name__ != "TransientExecutionError":
                        permanent_failure = True
                        break
                    if attempt >= self._execution_config.retry_limit:
                        break
                    delay = min(0.5 * (2**attempt), 5.0)
                    remaining = self._remaining_seconds()
                    if remaining is not None:
                        if remaining <= delay:
                            last_error = TimeoutError("Gateway operation deadline exceeded")
                            deadline_hit = True
                            break
                        delay = min(delay, max(0.0, remaining - 0.001))
                    await asyncio.sleep(delay)
                    continue
                else:
                    break
            if response is None:
                # One breaker verdict per model per request, after this
                # model's own retries, so retries do not double-count.
                self._record_failure(breaker_key, last_error)
                if deadline_hit or permanent_failure:
                    break
                continue
            if self._breaker is not None:
                self._breaker.record_success(breaker_key)
            response_cost, response_cost_estimated = self._response_cost(
                response, model_id, input_price, output_price, pricing_estimated
            )
            if stream is not None:
                stream.record_usage(
                    response,
                    task_id=subtask.task_id,
                    model_id=model_id,
                    cost=response_cost,
                    estimated=response_cost_estimated,
                )
            if _no_usable_output(response) and not (
                self._execution_config.hard_completion_token_limit is not None
                and response.output_evidence.finish_reason in {"length", "content_filter"}
            ):
                # e.g. a reasoning model that spent its whole budget thinking
                # (finish_reason=length): treat as this model failing.
                wasted_input_tokens += response.input_tokens
                wasted_output_tokens += response.output_tokens
                wasted_cost += float(response_cost)
                wasted_cost_estimated = wasted_cost_estimated or response_cost_estimated
                last_error = ModelUnavailableError(
                    f"Model '{provider_model or model_id}' returned no visible output "
                    f"(finish_reason={response.output_evidence.finish_reason})"
                )
                continue
            return ExecutionResult(
                task_id=subtask.task_id,
                status="success",
                result=response.text,
                selected_model=primary,
                executed_model=model_id if provider_model else response.model_id,
                fallback=index > 0,
                route=decision.tier,
                failure_type=type(last_error).__name__ if last_error else None,
                failure_reason=str(last_error) if last_error else None,
                latency_ms=(time.monotonic() - started) * 1000,
                confidence=decision.confidence,
                input_tokens=response.input_tokens + wasted_input_tokens,
                output_tokens=response.output_tokens + wasted_output_tokens,
                cost_usd=float(response_cost) + wasted_cost,
                cost_estimated=bool(response_cost_estimated or wasted_cost_estimated),
                provider=response.provider,
                usage_complete=response.usage_received and not had_unreceipted_attempt,
                final_input_tokens=response.input_tokens,
                final_output_tokens=response.output_tokens,
                final_usage_complete=response.usage_received,
                circuit_skipped=circuit_skipped,
                route_reason=decision.reason,
                route_shadow=decision.shadow,
                output_evidence=response.output_evidence,
            )
        if last_error is None and circuit_skipped and not attempted:
            failure_type = "CircuitOpen"
            failure_reason = f"All candidate models are temporarily unavailable (circuit open): {circuit_skipped}"
        else:
            failure_type = type(last_error).__name__ if last_error else "ProviderError"
            failure_reason = str(last_error) if last_error else "Provider execution failed"
        return ExecutionResult(
            task_id=subtask.task_id,
            status="failed",
            result=None,
            selected_model=primary,
            executed_model=None,
            fallback=bool(candidates and candidates[-1] != primary),
            route=decision.tier,
            failure_type=failure_type,
            failure_reason=failure_reason,
            latency_ms=(time.monotonic() - started) * 1000,
            confidence=decision.confidence,
            input_tokens=wasted_input_tokens,
            output_tokens=wasted_output_tokens,
            cost_usd=wasted_cost,
            cost_estimated=bool(wasted_cost_estimated),
            usage_complete=False,
            circuit_skipped=circuit_skipped,
            route_reason=decision.reason,
            route_shadow=decision.shadow,
        )

    def _response_cost(self, response, model_id, input_price, output_price, pricing_estimated) -> tuple[float, bool]:
        """Provider-reported cost when present, else tokens times catalog price."""
        cost = response.metadata.get("cost_usd")
        estimated = response.metadata.get("cost_estimated")
        if cost is not None:
            return float(cost), bool(estimated)
        if self._pricing is not None and self._pricing.get(model_id) is not None:
            return self._pricing.calculate_usage(
                model_id, response.input_tokens, response.output_tokens, response.output_evidence
            )
        if input_price is not None and output_price is not None:
            return (
                response.input_tokens / 1_000_000 * input_price + response.output_tokens / 1_000_000 * output_price
            ), bool(pricing_estimated)
        return 0.0, True

    def _record_failure(self, key: str, error: Exception | None) -> None:
        if self._breaker is None or error is None:
            return
        # Only transient provider trouble trips the breaker.
        if classify_error(error) is TransientExecutionError:
            self._breaker.record_failure(
                key, error=f"{type(error).__name__}: {error}"[:300], retry_after=retry_after_seconds(error)
            )
        else:
            # Not the model's fault (bad request/auth); free a probe slot
            # without judging the model either way.
            self._breaker.release_probe(key)

    def _record_prefetched(self, result: ExecutionResult) -> None:
        if self._breaker is None:
            return
        if result.status == "success" and result.executed_model:
            self._breaker.record_success(CircuitBreaker.key(result.executed_model))
        elif result.selected_model and result.failure_type == "TransientExecutionError":
            self._breaker.record_failure(CircuitBreaker.key(result.selected_model), error=result.failure_reason)

    def _remaining_seconds(self) -> float | None:
        if self._execution_config.deadline_at is None:
            return None
        return self._execution_config.deadline_at - time.monotonic()

    @staticmethod
    def _collect_upstream_results(subtask: SubTask, done: dict[str, ExecutionResult]) -> dict[str, str]:
        return {
            dependency_id: done[dependency_id].result
            for dependency_id in subtask.dependencies
            if dependency_id in done and done[dependency_id].result
        }

    async def aexecute_subtasks(
        self,
        subtasks: list[SubTask],
        *,
        original_prompt: str | None = None,
        request_context: dict[str, object] | None = None,
        constraints: object | None = None,
        context: ExecutionContext | None = None,
    ) -> list[ExecutionResult]:
        context = context or ExecutionContext(original_prompt=original_prompt, request_context=request_context)
        done: dict[str, ExecutionResult] = {}
        remaining = list(subtasks)
        while remaining:
            ready = [task for task in remaining if all(dependency in done for dependency in task.dependencies)]
            if not ready:
                raise ValueError(
                    f"Cyclic or unresolved dependencies among subtasks: {[task.task_id for task in remaining]}"
                )
            runnable = [
                task
                for task in ready
                if not any(done[dependency].status == "failed" for dependency in task.dependencies)
            ]
            blocked = [task for task in ready if task not in runnable]
            sequential = self._execution_config.stream_output or self._execution_config.shared_completion_budget
            if sequential:
                results = []
                for task in runnable:
                    result = await self.aexecute_subtask(
                        task, self._collect_upstream_results(task, done), constraints=constraints, context=context
                    )
                    results.append(result)
                    if (
                        self._execution_config.shared_completion_budget
                        and task.task_id in self._execution_config.stream_task_ids
                    ):
                        consumed = (
                            result.final_output_tokens
                            if result.final_output_tokens is not None
                            else result.output_tokens
                        )
                        remaining_budget = max(0, self._execution_config.hard_completion_token_limit - consumed)
                        self._execution_config = replace(
                            self._execution_config,
                            hard_completion_token_limit=remaining_budget,
                            max_output_tokens=min(self._execution_config.max_output_tokens, max(1, remaining_budget)),
                        )
            else:
                results = await asyncio.gather(
                    *(
                        self.aexecute_subtask(
                            task,
                            self._collect_upstream_results(task, done),
                            constraints=constraints,
                            context=context,
                        )
                        for task in runnable
                    )
                )
            for task, result in zip(runnable, results):
                done[task.task_id] = result
            for task in blocked:
                failed = [dependency for dependency in task.dependencies if done[dependency].status == "failed"]
                done[task.task_id] = ExecutionResult(
                    task_id=task.task_id,
                    status="failed",
                    result=None,
                    selected_model=None,
                    executed_model=None,
                    fallback=False,
                    failure_type="UpstreamFailure",
                    failure_reason=f"Dependencies failed: {failed}",
                )
            remaining = [task for task in remaining if task.task_id not in done]
        return [done[task.task_id] for task in subtasks]

    def execute_subtasks(self, subtasks: list[SubTask], **kwargs) -> list[ExecutionResult]:
        return asyncio.run(self.aexecute_subtasks(subtasks, **kwargs))


# Below this many visible tokens, a reply cut off by the token limit is a
# fragment of the model's thinking rather than an answer.
_MIN_VISIBLE_TOKENS_WHEN_TRUNCATED = 64


def _no_usable_output(response) -> bool:
    """Empty text, or a reply cut off by the token limit that is almost all
    reasoning (a long answer cut off near its end still counts as usable)."""
    if not (response.text or "").strip():
        return True
    evidence = response.output_evidence
    if evidence.finish_reason != "length" or evidence.reasoning_tokens is None:
        return False
    return response.output_tokens - evidence.reasoning_tokens < _MIN_VISIBLE_TOKENS_WHEN_TRUNCATED
