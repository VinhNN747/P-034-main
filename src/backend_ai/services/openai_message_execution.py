"""Single-producer SDK workflows retain messages and use database routing."""

import time
from dataclasses import asdict
from datetime import UTC, datetime

from src.backend_ai.agents.routing.router import ExecutionResult
from src.backend_ai.gateway.decomposition.no_decomposer import NoDecomposer
from src.backend_ai.gateway.observability.cost_tracker import summarize_costs
from src.backend_ai.gateway.observability.trace import TraceCollector, trace_timer
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.providers.base import GenerationOptions
from src.backend_ai.latency.context import current_latency_context
from src.backend_ai.latency.features import build_candidate_features
from src.backend_ai.services.runtime_stream_service import current_stream
from src.shared.dtos.gateway import AggregationResult, AggregationStatus, ExecutionResultTrace, SubtaskContribution
from src.shared.dtos.runtime_api import RuntimeAPIError


def requires_message_execution(payload):
    return bool(payload.tools or payload.n != 1 or payload.response_format or any(
        getattr(payload, key) is not None for key in
        ("temperature", "top_p", "stop", "seed", "frequency_penalty", "presence_penalty", "parallel_tool_calls"))
        or any(message.tool_calls or message.tool_call_id or message.refusal for message in payload.messages))


def preflight_options(payload, prepared):
    """Filter only eligibility; the database router still scores candidates."""
    from src.backend_ai.gateway.providers.openai import _is_reasoning_model
    requested = [key for key in ("temperature", "top_p", "stop", "seed", "frequency_penalty", "presence_penalty")
                 if getattr(payload, key) is not None]
    anthropic_unsupported = [key for key in ("temperature", "top_p", "seed", "frequency_penalty", "presence_penalty")
                             if getattr(payload, key) is not None]
    def permitted(row):
        _, model, provider = row
        identities = ({model.id,model.name}, {provider.id,provider.name,provider.kind})
        for constraints in (prepared.application_policy, prepared.request_constraints):
            for kind, names in zip(('models','providers'), identities):
                allowed = getattr(constraints,'allowed_'+kind)
                if allowed is not None and not names.intersection(allowed):
                    return False
                if names.intersection(getattr(constraints,'blocked_'+kind)):
                    return False
        return True
    candidates = [row for row in prepared.routing_snapshot.candidates if permitted(row)]
    eligible = []
    for row in candidates:
        _, model, provider = row
        if provider.kind in {"offline", "local"}:
            eligible.append(row)
            continue
        if requested and _is_reasoning_model(model.name):
            continue
        if provider.kind == "google" and (payload.tools or payload.n != 1 or
            any(message.tool_calls or message.tool_call_id or message.refusal for message in payload.messages) or
            any(key in requested for key in ("frequency_penalty", "presence_penalty"))):
            continue
        if provider.kind == "anthropic":
            if anthropic_unsupported or payload.n != 1:
                continue
            if payload.response_format and payload.response_format.get("type") != "json_schema":
                continue
            constraints = getattr(model, "feature_metadata_json", None) or {}
            constraints = constraints.get("constraints", {}) if isinstance(constraints, dict) else {}
            if payload.tools and "tool_use" not in model.capabilities_json:
                continue
            choice = payload.tool_choice
            forced = choice == "required" or isinstance(choice, dict) and choice.get("type") == "function"
            if forced and constraints.get("forced_tool_choice") not in {"conditional", "supported"}:
                continue
            from src.backend_ai.gateway.providers.anthropic_messages import validate_anthropic_json_schema
            try:
                if payload.response_format and payload.response_format.get("type") == "json_schema":
                    schema_format = payload.response_format.get("json_schema") or {}
                    validate_anthropic_json_schema(schema_format.get("schema"))
                for raw_tool in payload.tools or ():
                    tool = raw_tool if isinstance(raw_tool, dict) else raw_tool.model_dump(mode="json")
                    function = tool.get("function") or {}
                    if function.get("strict") is True:
                        validate_anthropic_json_schema(
                            function.get("parameters", function.get("input_schema"))
                        )
            except (TypeError, ValueError):
                continue
        if payload.response_format and payload.response_format.get("type") == "json_schema" and (
            "structured_output" not in model.capabilities_json and
            not model.name.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "gpt-6", "o3", "o4", "gemini-"))):
            continue
        eligible.append(row)
    if not eligible:
        param = (requested[0] if requested else "tools" if payload.tools else "n" if payload.n != 1
            else 'messages' if any(message.tool_calls or message.tool_call_id or message.refusal for message in payload.messages) else "response_format")
        raise RuntimeAPIError(400, "UNSUPPORTED_PARAMETER", "No eligible model supports this parameter profile.",
                              details={"param": param})
    return tuple(eligible)


async def execute_openai_messages(request, result, payload, prepared, router, deadline_at):
    # Use the semantic analysis/profile already computed by the native pipeline.
    # A single producer covers all goals and never rewrites the tool protocol.
    outcome = await NoDecomposer().decompose(request, result.semantic_analysis, result.profile,
        result.effective_constraints, "OpenAI message protocol requires one producer", result.goal_extraction)
    result.decomposition = RequestPipeline._attach_routing_signals(
        outcome.result, result.semantic_analysis, result.attachments, request)
    task = result.decomposition.subtasks[0]
    cap = result.effective_constraints.constraints.max_output_tokens or prepared.application_policy.max_output_tokens
    options = GenerationOptions(max_output_tokens=cap, hard_completion_token_limit=cap,
        messages=tuple(message.model_dump(mode="json", exclude_none=True) for message in payload.messages),
        temperature=payload.temperature, top_p=payload.top_p, stop=payload.stop, seed=payload.seed,
        frequency_penalty=payload.frequency_penalty, presence_penalty=payload.presence_penalty,
        response_format=payload.response_format, tools=tuple(payload.tools) if payload.tools else None,
        tool_choice=payload.tool_choice if payload.tools else None, parallel_tool_calls=payload.parallel_tool_calls,
        n=payload.n)
    # The analyzer's honest text surrogates replace media parts; preserve roles
    # and tool identifiers rather than serializing the entire conversation.
    if result.attachments:
        messages = [dict(message) for message in options.messages]
        for message_index, message in enumerate(messages):
            if isinstance(message.get("content"), list):
                parts = []
                for part in message["content"]:
                    if part["type"] == "text":
                        parts.append(part["text"])
                parts.extend(item.extracted_text for item in result.attachments
                    if item.message_index == message_index and item.status == "analyzed")
                message["content"] = "\n".join(parts)
        from dataclasses import replace
        options = replace(options, messages=tuple(messages))
    route_kwargs = {"constraints": result.effective_constraints.constraints}
    latency_context = current_latency_context()
    stream = current_stream()
    if latency_context is not None and latency_context.mode not in {"off", "disabled"}:
        task_type = getattr(task.task_type, "value", task.task_type)
        route_kwargs["latency_features"] = build_candidate_features(
            latency_context,
            task.task_id,
            material_chars=sum(len(str(message.get("content", ""))) + len(str(message.get("role", "")))
                               for message in options.messages),
            options=options,
            task_type=str(task_type),
            signals=task.routing_signals,
            now=datetime.now(UTC),
            protocol="openai_sdk",
            streaming=stream is not None,
            routing_objective=result.effective_constraints.constraints.quality_priority,
        )
    decision = await router.aroute(task, **route_kwargs)
    model_id = decision.selected_model
    provider = next((prepared.providers[key] for key in (model_id, decision.provider_name)
                     if key in prepared.providers), None)
    if provider is None:
        raise RuntimeAPIError(503, "PROVIDER_UNAVAILABLE", "The selected provider is unavailable.")
    started, timer = trace_timer()
    timeout = min(prepared.config.timeout_seconds, max(0.001, deadline_at - time.monotonic()))
    kwargs = dict(task_id=task.task_id, model_id=decision.provider_model or model_id,
                  timeout_seconds=timeout, options=options)
    if stream is not None:
        stream.model = kwargs["model_id"]
        response = None
        async for event in provider.stream(request.focus_text(), **kwargs):
            if event.final is not None:
                cost, estimated = prepared.pricing.calculate_usage(
                    model_id, event.final.input_tokens, event.final.output_tokens, event.final.output_evidence
                )
                stream.record_usage(event.final, task_id=task.task_id, model_id=model_id, cost=cost, estimated=estimated)
            if event.type == "final":
                response = event.final
            else:
                await stream.emit(event)
        if response is None:
            raise RuntimeError("Provider stream ended without its final receipt")
    else:
        response = await provider.generate(request.focus_text(), **kwargs)
    cost, estimated = prepared.pricing.calculate_usage(
        model_id, response.input_tokens, response.output_tokens, response.output_evidence
    )
    execution = ExecutionResult(task_id=task.task_id, status="success", result=response.text,
        selected_model=model_id, executed_model=model_id, fallback=False, provider=decision.provider_name,
        input_tokens=response.input_tokens, output_tokens=response.output_tokens, cost_usd=cost,
        cost_estimated=estimated, usage_complete=response.usage_received, output_evidence=response.output_evidence,
        route_reason=decision.reason, route_shadow=decision.shadow,
        final_input_tokens=response.input_tokens, final_output_tokens=response.output_tokens,
        final_usage_complete=response.usage_received, latency_ms=response.latency_ms)
    result.execution_results = [ExecutionResultTrace(**asdict(execution))]
    result.openai_choices = response.metadata.get("openai_choices")
    result.aggregation = AggregationResult(original_request_id=request.request_id, status=AggregationStatus.COMPLETE,
        final_answer=response.text, contributions=[SubtaskContribution(task_id=task.task_id, goal_ids=task.goal_ids,
            status="success", used_in_final_answer=True)], strategy="openai_messages",
        reason="Single producer preserves messages/options/tool protocol.", output_evidence=response.output_evidence)
    result.output_evidence = response.output_evidence
    collector = TraceCollector(request.request_id)
    collector.add(stage=f"route_execute:{task.task_id}", strategy="openai_messages", model=model_id, started_at=started,
        timer_started=timer, input_tokens=response.input_tokens, output_tokens=response.output_tokens,
        cost_usd=cost, metadata={"usage_complete": response.usage_received, "cost_estimated": estimated,
                                "route_reason": decision.reason, "route_shadow": decision.shadow})
    result.traces.extend(collector.traces)
    result.costs = summarize_costs(result.traces)
    return result
