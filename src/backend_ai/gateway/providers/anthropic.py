"""Direct Anthropic Messages API adapter using the official async SDK."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any

from src.backend_ai.gateway.providers.anthropic_messages import (
    build_anthropic_request,
    normalize_message_response,
    stream_receipt,
)
from src.backend_ai.gateway.providers.anthropic_token_count import count_anthropic_tokens
from src.backend_ai.gateway.providers.base import (
    GenerationOptions,
    ModelUnavailableError,
    PermanentProviderError,
    ProviderClient,
    ProviderConfigurationError,
    ProviderOutputError,
    ProviderResponse,
    ProviderStreamEvent,
    TransientProviderError,
)


class AnthropicProvider(ProviderClient):
    name = "anthropic"

    def __init__(self, *, api_key: str, enabled: bool = True, base_url: str | None = None,
                 model_profiles: dict[str, Any] | None = None, client: Any = None) -> None:
        if not enabled:
            raise ProviderConfigurationError("Anthropic provider is disabled")
        key = (api_key or "").strip()
        if not key:
            raise ProviderConfigurationError("The Anthropic provider credential is not available")
        self._api_key = key
        self._base_url = base_url
        self._model_profiles = dict(model_profiles or {})
        self._injected_client = client

    async def generate(self, prompt: str, *, task_id: str, model_id: str, timeout_seconds: float,
                       options: GenerationOptions | None = None) -> ProviderResponse:
        request = self._request(prompt, model_id, options)
        started = perf_counter()
        try:
            async with self._client(timeout_seconds) as client:
                response = await client.messages.create(**request, stream=False)
        except ProviderConfigurationError:
            raise
        except Exception as exc:
            raise _map_error(exc) from exc
        try:
            return normalize_message_response(response, model_id=model_id, task_id=task_id, started=started)
        except ProviderConfigurationError:
            raise

    async def count_tokens(self, prompt: str, *, task_id: str, model_id: str, timeout_seconds: float,
                           options: GenerationOptions | None = None) -> int | None:
        """Count input tokens through Anthropic's non-generative endpoint."""
        request = self._request(prompt, model_id, options)
        async with self._client(timeout_seconds) as client:
            return await count_anthropic_tokens(client, request)

    async def stream(self, prompt: str, *, task_id: str, model_id: str, timeout_seconds: float,
                     options: GenerationOptions | None = None):
        request = self._request(prompt, model_id, options)
        started = perf_counter()
        text_parts: list[str] = []
        tools: dict[int, dict[str, Any]] = {}
        tool_indexes: dict[int, int] = {}
        input_usage: dict[str, int | None] = {
            "input_tokens": None,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_creation_5m_input_tokens": None,
            "cache_creation_1h_input_tokens": None,
        }
        output_tokens: int | None = None
        stop_reason: str | None = None
        usage_received = False
        terminal_seen = False
        upstream = None
        try:
            async with self._client(timeout_seconds) as client:
                upstream = await client.messages.create(**request, stream=True)
                async for event in upstream:
                    kind = _get(event, "type")
                    if kind == "message_start":
                        usage = _get(_get(event, "message", {}), "usage", {}) or {}
                        input_usage.update(_usage_details(usage))
                        usage_received = input_usage["input_tokens"] is not None
                        if _has_usage(input_usage):
                            receipt = stream_receipt(text_parts=text_parts, tools=tools, input_usage=input_usage,
                                output_tokens=output_tokens, stop_reason=stop_reason, model_id=model_id,
                                task_id=task_id, started=started, usage_received=usage_received, allow_empty=True)
                            yield ProviderStreamEvent(type="usage", final=receipt)
                    elif kind == "content_block_start":
                        block_index = _get(event, "index")
                        block = _get(event, "content_block", {}) or {}
                        if _get(block, "type") == "tool_use":
                            call_id, name = _get(block, "id"), _get(block, "name")
                            if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
                                index = len(tool_indexes)
                                tool_indexes[block_index] = index
                                tools[index] = {"id": call_id, "name": name,
                                    "input": _get(block, "input", {}) or {}, "arguments": ""}
                                yield ProviderStreamEvent(type="tool_delta", index=index,
                                    tool_delta={"index": index, "id": call_id, "type": "function",
                                        "function": {"name": name, "arguments": ""}})
                        elif _get(block, "type") == "text":
                            text = _get(block, "text")
                            if isinstance(text, str) and text:
                                text_parts.append(text)
                                yield ProviderStreamEvent(type="text_delta", text=text)
                    elif kind == "content_block_delta":
                        block_index = _get(event, "index")
                        delta = _get(event, "delta", {}) or {}
                        delta_kind = _get(delta, "type")
                        if delta_kind == "text_delta":
                            text = _get(delta, "text")
                            if isinstance(text, str) and text:
                                text_parts.append(text)
                                yield ProviderStreamEvent(type="text_delta", text=text)
                        elif delta_kind == "input_json_delta" and block_index in tool_indexes:
                            partial = _get(delta, "partial_json")
                            if isinstance(partial, str) and partial:
                                tool = tools[tool_indexes[block_index]]
                                tool["arguments"] += partial
                                yield ProviderStreamEvent(type="tool_delta", index=tool_indexes[block_index],
                                    tool_delta={"index": tool_indexes[block_index],
                                        "function": {"arguments": partial}})
                        # Thinking and signature events are intentionally ignored.
                    elif kind == "message_delta":
                        delta = _get(event, "delta", {}) or {}
                        stop_reason = _get(delta, "stop_reason") or stop_reason
                        usage = _get(event, "usage", {}) or {}
                        output_tokens = _get(usage, "output_tokens", output_tokens)
                        usage_received = usage_received and type(output_tokens) is int and output_tokens >= 0
                        if _has_usage(input_usage) or type(output_tokens) is int and output_tokens >= 0:
                            receipt = stream_receipt(text_parts=text_parts, tools=tools, input_usage=input_usage,
                                output_tokens=output_tokens, stop_reason=stop_reason, model_id=model_id,
                                task_id=task_id, started=started, usage_received=usage_received, allow_empty=True)
                            yield ProviderStreamEvent(type="usage", final=receipt)
                    elif kind == "message_stop":
                        terminal_seen = True
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, Exception):
                raise _map_error(exc) from exc
            raise
        finally:
            await _close_stream(upstream)

        if not terminal_seen:
            receipt = stream_receipt(text_parts=text_parts, tools=tools, input_usage=input_usage,
                output_tokens=output_tokens, stop_reason=stop_reason, model_id=model_id, task_id=task_id,
                started=started, usage_received=usage_received, allow_empty=True)
            raise ProviderOutputError("OUTPUT_INCOMPLETE_STREAM",
                "Anthropic ended its stream before the terminal message event.", receipt)

        for index in sorted(tools):
            tool = tools[index]
            if tool["arguments"]:
                try:
                    parsed = json.loads(tool["arguments"])
                except (ValueError, TypeError) as exc:
                    receipt = stream_receipt(text_parts=text_parts, tools=tools, input_usage=input_usage,
                        output_tokens=output_tokens, stop_reason=stop_reason, model_id=model_id, task_id=task_id,
                        started=started, usage_received=usage_received)
                    raise ProviderOutputError("OUTPUT_INVALID_TOOL_JSON",
                        "Anthropic returned an incomplete streamed tool input.", receipt) from exc
                if not isinstance(parsed, dict):
                    receipt = stream_receipt(text_parts=text_parts, tools=tools, input_usage=input_usage,
                        output_tokens=output_tokens, stop_reason=stop_reason, model_id=model_id, task_id=task_id,
                        started=started, usage_received=usage_received)
                    raise ProviderOutputError("OUTPUT_INVALID_TOOL_JSON",
                        "Anthropic returned a non-object streamed tool input.", receipt)
                tool["input"] = parsed
        result = stream_receipt(text_parts=text_parts, tools=tools, input_usage=input_usage,
            output_tokens=output_tokens, stop_reason=stop_reason, model_id=model_id, task_id=task_id,
            started=started, usage_received=usage_received)
        # A single final receipt avoids billing the same usage from an interim
        # usage event and the final event in the gateway stream collector.
        yield ProviderStreamEvent(type="final", final=result)

    def _request(self, prompt: str, model_id: str, options: GenerationOptions | None) -> dict[str, Any]:
        profile = self._model_profiles.get(model_id)
        if profile is None:
            raise ProviderConfigurationError("Anthropic request is missing its database model profile")
        try:
            return build_anthropic_request(model_id, prompt, options, profile)
        except (TypeError, ValueError) as exc:
            raise ProviderConfigurationError(str(exc)) from exc

    @asynccontextmanager
    async def _client(self, timeout_seconds: float):
        if self._injected_client is not None:
            yield self._injected_client
            return
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - dependency install catches this earlier
            raise ProviderConfigurationError("anthropic is required for the Anthropic provider") from exc
        client = AsyncAnthropic(api_key=self._api_key, base_url=self._base_url, timeout=timeout_seconds,
            max_retries=0)
        async with client as sdk_client:
            yield sdk_client


def _usage_details(usage: Any) -> dict[str, int | None]:
    from src.backend_ai.gateway.providers.anthropic_messages import _usage_details as normalize
    return normalize(usage)


def _has_usage(usage: dict[str, int | None]) -> bool:
    return any(type(value) is int and value >= 0 for value in usage.values())


async def _close_stream(stream: Any) -> None:
    if stream is None:
        return
    close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
    if close is None:
        return
    result = close()
    if hasattr(result, "__await__"):
        await result


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _map_error(exc: Exception) -> Exception:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = None
    try:
        retry_after = float(headers.get("retry-after")) if headers.get("retry-after") is not None else None
    except (TypeError, ValueError):
        pass
    message = "Anthropic API request failed."
    if status == 404:
        return ModelUnavailableError(message, status_code=status)
    if status in {408, 429, 529} or isinstance(status, int) and status >= 500:
        return TransientProviderError(message, status_code=status, retry_after=retry_after)
    if status is None and (isinstance(exc, TimeoutError) or any(word in type(exc).__name__.lower()
            for word in ("timeout", "connection", "network"))):
        return TransientProviderError("Anthropic request timed out or could not connect.")
    return PermanentProviderError(message, status_code=status)
