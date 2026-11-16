"""OpenAI message protocol without LangChain's prompt flattening."""

from time import perf_counter

from src.backend_ai.gateway.providers.base import PermanentProviderError, ProviderResponse, ProviderStreamEvent
from src.shared.dtos.gateway import OutputEvidence


def request_options(model_id, options, *, max_tokens_field=False):
    args = {"model": model_id, "messages": list(options.messages), "n": options.n}
    cap = options.max_output_tokens
    if options.hard_completion_token_limit is not None:
        cap = min(cap, options.hard_completion_token_limit) if cap is not None else options.hard_completion_token_limit
    if cap is not None:
        legacy = max_tokens_field or model_id.startswith("gpt-3.5")
        args["max_tokens" if legacy else "max_completion_tokens"] = cap
    for key in ("temperature", "top_p", "stop", "seed", "frequency_penalty", "presence_penalty", "tool_choice",
                "parallel_tool_calls", "response_format"):
        value = getattr(options, key)
        if value is not None:
            args[key] = value
    if options.tools:
        args["tools"] = list(options.tools)
    return args


def normalized_response(choices, usage, *, model_id, task_id, started, provider_name="openai"):
    if not choices or [choice["index"] for choice in choices] != list(range(len(choices))):
        raise PermanentProviderError("Provider returned invalid choice indexes")
    # A forced function call can arrive with `stop`. Preserve truncation and
    # filtering, but expose a completed call with the Chat protocol reason.
    choices = [{**choice, "finish_reason": "tool_calls"}
               if choice["finish_reason"] == "stop" and choice["message"].get("tool_calls")
               else choice for choice in choices]
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    evidence = OutputEvidence(finish_reason=choices[0]["finish_reason"] if choices[0]["finish_reason"] in
        {"stop", "length", "content_filter", "tool_calls"} else "unknown",
        cached_tokens=getattr(prompt_details, "cached_tokens", None),
        cache_write_tokens=getattr(prompt_details, "cache_write_tokens", None),
        reasoning_tokens=getattr(completion_details, "reasoning_tokens", None))
    return ProviderResponse(text=choices[0]["message"].get("content") or "", provider=provider_name, model_id=model_id,
        input_tokens=prompt if type(prompt) is int else 0, output_tokens=completion if type(completion) is int else 0,
        latency_ms=(perf_counter() - started) * 1000, output_evidence=evidence,
        usage_received=type(prompt) is int and type(completion) is int,
        metadata={"task_id": task_id, "mode": "live", "openai_choices": choices})


async def generate_messages(client, options, *, model_id, task_id, upstream_model=None, provider_name="openai",
                            max_tokens_field=False):
    started = perf_counter()
    response = await client.chat.completions.create(
        **request_options(upstream_model or model_id, options, max_tokens_field=max_tokens_field))
    choices = [{"index": choice.index, "message": choice.message.model_dump(mode="json"),
                "finish_reason": choice.finish_reason} for choice in response.choices]
    return normalized_response(choices, response.usage, model_id=model_id, task_id=task_id, started=started,
                               provider_name=provider_name)


async def stream_messages(client, options, *, model_id, task_id, upstream_model=None, provider_name="openai",
                          max_tokens_field=False):
    started = perf_counter()
    upstream = await client.chat.completions.create(
        **request_options(upstream_model or model_id, options, max_tokens_field=max_tokens_field), stream=True,
        stream_options={"include_usage": True})
    choices = {}
    tools = {}
    usage = None
    try:
        async for chunk in upstream:
            if chunk.usage is not None:
                usage = chunk.usage
                if type(usage.prompt_tokens) is int and type(usage.completion_tokens) is int:
                    yield ProviderStreamEvent(type='usage', final=ProviderResponse(text='', provider=provider_name,
                        model_id=model_id, input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens,
                        usage_received=True))
            for choice in chunk.choices:
                saved = choices.setdefault(choice.index, {"index": choice.index,
                    "message": {"role": "assistant", "content": None}, "finish_reason": "unknown"})
                delta = choice.delta
                if delta.content is not None:
                    saved["message"]["content"] = (saved["message"]["content"] or "") + delta.content
                    if delta.content:
                        yield ProviderStreamEvent(type="text_delta", index=choice.index, text=delta.content)
                if delta.refusal is not None:
                    saved["message"]["refusal"] = saved["message"].get("refusal", "") + delta.refusal
                    yield ProviderStreamEvent(type="refusal_delta", index=choice.index, text=delta.refusal)
                for tool in delta.tool_calls or []:
                    assembled = tools.setdefault((choice.index, tool.index),
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tool.id:
                        assembled["id"] += tool.id
                    if tool.function:
                        for key in ("name", "arguments"):
                            value = getattr(tool.function, key, None)
                            if value:
                                assembled["function"][key] += value
                    yield ProviderStreamEvent(type="tool_delta", index=choice.index,
                        tool_delta=tool.model_dump(mode="json", exclude_none=True))
                if choice.finish_reason is not None:
                    saved["finish_reason"] = choice.finish_reason
    finally:
        await upstream.close()
    for index, choice in choices.items():
        calls = [tool for (choice_index, _), tool in sorted(tools.items()) if choice_index == index]
        if calls:
            choice["message"]["tool_calls"] = calls
    final = normalized_response([choices[index] for index in sorted(choices)], usage,
        model_id=model_id, task_id=task_id, started=started, provider_name=provider_name)
    yield ProviderStreamEvent(type="final", final=final)
