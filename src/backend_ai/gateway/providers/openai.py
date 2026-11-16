from __future__ import annotations

from time import perf_counter
from typing import Any

from src.backend_ai.gateway.providers.base import (
    REASONING_TOKEN_HEADROOM,
    GenerationOptions,
    ModelUnavailableError,
    PermanentProviderError,
    ProviderClient,
    ProviderConfigurationError,
    ProviderResponse,
    ProviderStreamEvent,
    TransientProviderError,
)
from src.shared.dtos.gateway import OutputEvidence

# Reasoning models get REASONING_TOKEN_HEADROOM and a low reasoning effort.
_REASONING_MODEL_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
REASONING_EFFORT = "low"
# A catalog name "<model>@<effort>" is the same upstream model at another
# reasoning effort (LiteLLM-style alias), so one model can fill two tiers.
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def split_effort(model_id: str) -> tuple[str, str | None]:
    """("gpt-6.1-sol@high") -> ("gpt-6.1-sol", "high"); names without a known effort pass through."""
    base, sep, effort = model_id.rpartition("@")
    if sep and base and effort in REASONING_EFFORTS:
        return base, effort
    return model_id, None


def _is_reasoning_model(model_id: str) -> bool:
    return model_id.startswith(_REASONING_MODEL_PREFIXES) and "chat" not in model_id


class OpenAIProvider(ProviderClient):
    """Opt-in OpenAI adapter; no client is created in offline mode."""

    name = "openai"
    sends_reasoning_effort = True
    # Self-hosted servers (Ollama, llama.cpp) only read the classic field, not max_completion_tokens.
    uses_max_tokens_field = False

    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        if not enabled:
            raise ProviderConfigurationError("OpenAI provider is disabled")
        api_key = (api_key or "").strip()
        if not api_key:
            raise ProviderConfigurationError("OpenAI API key was not supplied")
        self._api_key = api_key
        self._base_url = base_url
        self._clients: dict[tuple[str, float, int | None, int | None], Any] = {}

    def _upstream_model(self, model_id: str) -> str:
        """The model name the upstream API expects for a catalog model id."""
        return split_effort(model_id)[0]

    def _is_reasoning(self, model_id: str) -> bool:
        return _is_reasoning_model(model_id)

    def _client_for(self, model_id: str, timeout_seconds: float, max_output_tokens: int | None = None, *,
                    hard_completion_token_limit: int | None = None):
        cache_key = (model_id, timeout_seconds, max_output_tokens, hard_completion_token_limit)
        if cache_key not in self._clients:
            try:
                from langchain_openai import ChatOpenAI
            except ImportError as exc:  # pragma: no cover - dependency setup catches this earlier
                raise ProviderConfigurationError("langchain-openai is required for the OpenAI provider") from exc
            kwargs: dict[str, Any] = {
                "api_key": self._api_key,
                "model": self._upstream_model(model_id),
                "timeout": timeout_seconds,
                # Runtime owns retries so operation state and billing stay aligned.
                "max_retries": 0,
            }
            reasoning = self._is_reasoning(model_id)
            if reasoning and self.sends_reasoning_effort:
                kwargs["reasoning_effort"] = split_effort(model_id)[1] or REASONING_EFFORT
            if max_output_tokens is not None:
                # ChatOpenAI maps this abstraction to max_completion_tokens
                # for current models that use the newer API field.
                cap = (
                    min(max_output_tokens, hard_completion_token_limit)
                    if hard_completion_token_limit is not None
                    else max_output_tokens + (REASONING_TOKEN_HEADROOM if reasoning else 0)
                )
                if self.uses_max_tokens_field:
                    kwargs["extra_body"] = {"max_tokens": cap}
                else:
                    kwargs["max_tokens"] = cap
            if self._base_url:
                kwargs["base_url"] = self._base_url
            http_client = self._http_client()
            if http_client is not None:
                kwargs["http_async_client"] = http_client
            self._clients[cache_key] = ChatOpenAI(**kwargs)
        return self._clients[cache_key]

    def _http_client(self):
        """An HTTP client for the SDK, or None for the SDK default (subclasses tighten it)."""
        return None

    def _sdk_client(self, timeout_seconds: float):
        from openai import AsyncOpenAI

        http_client = self._http_client()
        return AsyncOpenAI(api_key=self._api_key, base_url=self._base_url, timeout=timeout_seconds, max_retries=0,
                           **({"http_client": http_client} if http_client is not None else {}))

    async def generate(
        self,
        prompt: str,
        *,
        task_id: str,
        model_id: str,
        timeout_seconds: float,
        options: GenerationOptions | None = None,
    ) -> ProviderResponse:
        if options is not None and options.messages is not None:
            from src.backend_ai.gateway.providers.openai_messages import generate_messages
            async with self._sdk_client(timeout_seconds) as client:
                return await generate_messages(client, options, model_id=model_id, task_id=task_id,
                    upstream_model=self._upstream_model(model_id), provider_name=self.name,
                    max_tokens_field=self.uses_max_tokens_field)
        started = perf_counter()
        try:
            client = self._client_for(
                model_id, timeout_seconds, options.max_output_tokens if options else None,
                hard_completion_token_limit=options.hard_completion_token_limit if options else None,
            )
            if options and options.json_schema is not None:
                # JSON mode also supports older catalog models such as GPT-3.5.
                # The caller validates the exact score schema independently.
                client = client.bind(response_format={"type": "json_object"})
            response = await client.ainvoke(prompt)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if _is_model_unavailable(exc):
                raise ModelUnavailableError(str(exc), status_code=status) from exc
            if _is_transient(exc):
                raise TransientProviderError(str(exc), retry_after=_retry_after(exc), status_code=status) from exc
            raise PermanentProviderError(str(exc), status_code=status) from exc

        usage = _usage_from_response(response)
        return ProviderResponse(
            # LangChain can expose Responses API output as a list containing
            # reasoning blocks plus the final text block. Never stringify the
            # whole content: that can leak encrypted reasoning payloads to the UI.
            text=_text_from_response(response),
            provider=self.name,
            model_id=model_id,
            input_tokens=usage[0],
            output_tokens=usage[1],
            latency_ms=(perf_counter() - started) * 1000,
            metadata={
                "task_id": task_id, "mode": "live",
                "finish_reason": getattr(response, "response_metadata", {}).get("finish_reason"),
            },
            usage_received=_has_usage(response),
            output_evidence=_output_evidence(response),
        )

    async def stream(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        if options is not None and options.messages is not None:
            from src.backend_ai.gateway.providers.openai_messages import stream_messages
            async with self._sdk_client(timeout_seconds) as client:
                async for event in stream_messages(client, options, model_id=model_id, task_id=task_id,
                    upstream_model=self._upstream_model(model_id), provider_name=self.name,
                    max_tokens_field=self.uses_max_tokens_field):
                    yield event
            return
        started = perf_counter()
        client = self._client_for(model_id, timeout_seconds, options.max_output_tokens if options else None,
            hard_completion_token_limit=options.hard_completion_token_limit if options else None)
        if options and options.json_schema is not None:
            client = client.bind(response_format={"type": "json_object"})
        upstream = client.astream(prompt, stream_usage=True)
        combined = None
        text_parts = []
        try:
            async for chunk in upstream:
                combined = chunk if combined is None else combined + chunk
                if _has_usage(combined):
                    inputs, outputs = _usage_from_response(combined)
                    yield ProviderStreamEvent(type='usage', final=ProviderResponse(text='', provider=self.name,
                        model_id=model_id, input_tokens=inputs, output_tokens=outputs, usage_received=True,
                        output_evidence=_output_evidence(combined)))
                text = "".join(_text_blocks_from_content(chunk.content))
                if text:
                    text_parts.append(text)
                    yield ProviderStreamEvent(type="text_delta", text=text)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            error = TransientProviderError if _is_transient(exc) else PermanentProviderError
            raise error("The streaming provider request failed.", status_code=status) from exc
        finally:
            await upstream.aclose()
        if combined is None:
            raise PermanentProviderError("The provider returned an empty stream")
        inputs, outputs = _usage_from_response(combined)
        yield ProviderStreamEvent(type="final", final=ProviderResponse(
            text="".join(text_parts), provider=self.name, model_id=model_id,
            input_tokens=inputs, output_tokens=outputs, latency_ms=(perf_counter() - started) * 1000,
            metadata={"task_id": task_id, "mode": "live"}, usage_received=_has_usage(combined),
            output_evidence=_output_evidence(combined)))


def _usage_from_response(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None) or getattr(response, "response_metadata", {}).get(
        "token_usage", {}
    )
    return int(usage.get("input_tokens", usage.get("prompt_tokens", 0))), int(
        usage.get("output_tokens", usage.get("completion_tokens", 0))
    )


def _has_usage(response: Any) -> bool:
    usage = getattr(response, "usage_metadata", None) or getattr(response, "response_metadata", {}).get(
        "token_usage", {}
    )
    if not isinstance(usage, dict):
        return False
    inputs = usage.get('input_tokens', usage.get('prompt_tokens'))
    outputs = usage.get('output_tokens', usage.get('completion_tokens'))
    return all(type(value) is int and value >= 0 for value in (inputs, outputs))


def _text_from_response(response: Any) -> str:
    """Return only user-visible text blocks from an OpenAI/LangChain response.

    Newer Responses API messages may contain reasoning blocks with fields such
    as ``encrypted_content`` alongside the final ``text`` block. Only known
    text fields are traversed so internal provider payloads cannot be rendered
    accidentally in Playground.
    """

    content = getattr(response, "content", response)
    text_blocks = _text_blocks_from_content(content)
    return (text_blocks[-1] if text_blocks else "").strip()


def _text_blocks_from_content(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, (list, tuple)):
        blocks: list[str] = []
        for item in content:
            blocks.extend(_text_blocks_from_content(item))
        return blocks
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return [text]
        nested = content.get("content")
        if isinstance(nested, (list, tuple)):
            return _text_blocks_from_content(nested)
        return []

    text = getattr(content, "text", None)
    return [text] if isinstance(text, str) else []


def _is_model_unavailable(error: Exception) -> bool:
    """404 / model_not_found: a wrong or retired model id in the catalog."""
    return getattr(error, "status_code", None) == 404 or "model_not_found" in str(error)


def _is_transient(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    if status is not None:
        return status == 408 or status == 429 or status >= 500
    name = type(error).__name__.lower()
    return "timeout" in name or "connection" in name or "rate" in name


def _retry_after(error: Exception) -> float | None:
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    value = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _output_evidence(response: Any) -> OutputEvidence:
    metadata = getattr(response, "response_metadata", None) or {}
    usage_metadata = getattr(response, "usage_metadata", None) or {}
    token_usage = metadata.get("token_usage", {}) if isinstance(metadata, dict) else {}
    prompt_details = _dict_value(token_usage, "prompt_tokens_details")
    completion_details = _dict_value(token_usage, "completion_tokens_details")
    input_details = _dict_value(usage_metadata, "input_token_details")
    output_details = _dict_value(usage_metadata, "output_token_details")
    reason = metadata.get("finish_reason") or metadata.get("stop_reason") if isinstance(metadata, dict) else None
    reason_map = {
        "stop": "stop", "end_turn": "stop", "STOP": "stop",
        "length": "length", "max_tokens": "length", "MAX_TOKENS": "length",
        "content_filter": "content_filter", "CONTENT_FILTER": "content_filter", "SAFETY": "content_filter",
    }
    return OutputEvidence(
        finish_reason=reason_map.get(reason, "unknown"),
        cached_tokens=_first_int(prompt_details.get("cached_tokens"), input_details.get("cache_read")),
        cache_write_tokens=_first_int(
            prompt_details.get("cache_write_tokens"), input_details.get("cache_creation")
        ),
        reasoning_tokens=_first_int(
            completion_details.get("reasoning_tokens"), output_details.get("reasoning")
        ),
    )


def _dict_value(source: Any, key: str) -> dict:
    if not isinstance(source, dict):
        return {}
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def _first_int(*values: Any) -> int | None:
    for value in values:
        if type(value) is int and value >= 0:
            return value
    return None
