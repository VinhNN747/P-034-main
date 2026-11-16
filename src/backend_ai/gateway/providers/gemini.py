from __future__ import annotations

import json
from time import perf_counter
from urllib.parse import quote

import httpx

from src.backend_ai.gateway.providers.base import (
    REASONING_TOKEN_HEADROOM,
    GenerationOptions,
    PermanentProviderError,
    ProviderClient,
    ProviderConfigurationError,
    ProviderOutputError,
    ProviderResponse,
    ProviderStreamEvent,
    TransientProviderError,
)
from src.shared.dtos.gateway import OutputEvidence


class GeminiProvider(ProviderClient):
    """Google Gemini Developer API adapter using its documented REST endpoint."""

    name = "google"
    _API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, *, api_key: str, enabled: bool = True) -> None:
        normalized_key = api_key.strip()
        if not enabled:
            raise ProviderConfigurationError("Gemini provider is disabled")
        if not normalized_key:
            raise ProviderConfigurationError("The Google provider credential is not available")
        self._api_key = normalized_key

    async def generate(
        self,
        prompt: str,
        *,
        task_id: str,
        model_id: str,
        timeout_seconds: float,
        options: GenerationOptions | None = None,
    ) -> ProviderResponse:
        model_name = model_id.removeprefix("models/")
        endpoint = f"{self._API_ROOT}/{quote(model_name, safe='')}:generateContent"
        payload = _generation_payload(prompt, model_name, options)
        started = perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    endpoint,
                    headers={"x-goog-api-key": self._api_key},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise TransientProviderError("Gemini request timed out") from exc
        except httpx.RequestError as exc:
            raise TransientProviderError(f"Gemini network request failed: {exc}") from exc

        if response.is_error:
            message = _error_message(response)
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise TransientProviderError(message)
            raise PermanentProviderError(message)

        try:
            data = response.json()
        except ValueError as exc:
            raise PermanentProviderError("Gemini returned invalid JSON") from exc

        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts") or [] if candidates else []
        text = "".join(
            part.get("text", "") for part in parts if isinstance(part.get("text"), str) and not part.get("thought")
        ).strip()
        finish_reason = candidates[0].get("finishReason") if candidates else None

        usage = data.get("usageMetadata") or {}
        input_tokens = int(usage.get("promptTokenCount", 0) or 0)
        candidate_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
        thought_tokens = int(usage.get("thoughtsTokenCount", 0) or 0)
        result = ProviderResponse(
            text=text,
            provider=self.name,
            model_id=model_name,
            input_tokens=input_tokens,
            # Google bills thinking tokens at the output-token rate.
            output_tokens=candidate_tokens + thought_tokens,
            latency_ms=(perf_counter() - started) * 1000,
            metadata={
                "task_id": task_id,
                "mode": "live",
                "thinking_tokens": thought_tokens,
                "finish_reason": finish_reason,
            },
            usage_received=_valid_usage(usage),
            output_evidence=OutputEvidence(
                finish_reason=_finish_reason(candidates[0].get("finishReason") if candidates else None),
                cached_tokens=_nonnegative_int(usage.get("cachedContentTokenCount")),
                reasoning_tokens=_nonnegative_int(usage.get("thoughtsTokenCount")),
            ),
        )
        if not text and not (options and options.hard_completion_token_limit is not None
                             and result.output_evidence.finish_reason in {"length", "content_filter"}):
            if finish_reason == "MAX_TOKENS":
                raise ProviderOutputError(
                    "OUTPUT_TOKEN_LIMIT", "The model exhausted its token limit before returning an answer.", result
                )
            if (data.get("promptFeedback") or {}).get("blockReason") or finish_reason in {
                "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
            }:
                raise ProviderOutputError("OUTPUT_BLOCKED", "The provider blocked the model output.", result)
            raise ProviderOutputError("OUTPUT_EMPTY", "The provider returned no answer text.", result)
        return result

    async def stream(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        model_name = model_id.removeprefix("models/")
        endpoint = f"{self._API_ROOT}/{quote(model_name, safe='')}:streamGenerateContent"
        started = perf_counter()
        text_parts = []
        usage = {}
        reason = None
        received_usage = False
        def receipt():
            return ProviderResponse(text=''.join(text_parts), provider=self.name, model_id=model_name,
                input_tokens=_nonnegative_int(usage.get('promptTokenCount')) or 0,
                output_tokens=(_nonnegative_int(usage.get('candidatesTokenCount')) or 0)
                    + (_nonnegative_int(usage.get('thoughtsTokenCount')) or 0),
                latency_ms=(perf_counter() - started) * 1000, metadata={'task_id': task_id, 'mode': 'live'},
                usage_received=received_usage and _valid_usage(usage),
                output_evidence=OutputEvidence(finish_reason=_finish_reason(reason),
                    cached_tokens=_nonnegative_int(usage.get('cachedContentTokenCount')),
                    reasoning_tokens=_nonnegative_int(usage.get('thoughtsTokenCount'))))

        def frame(raw):
            nonlocal usage, reason, received_usage
            data = json.loads(raw)
            candidates = data.get('candidates') or []
            first = candidates[0] if candidates else {}
            reason = first.get('finishReason') or reason
            events = []
            if isinstance(data.get('usageMetadata'), dict):
                usage = data['usageMetadata']
                received_usage = _valid_usage(usage)
                if received_usage:
                    events.append(ProviderStreamEvent(type='usage', final=receipt()))
            text = ''.join(part.get('text', '') for part in (first.get('content') or {}).get('parts', [])
                if isinstance(part.get('text'), str) and not part.get('thought'))
            if text:
                text_parts.append(text)
                events.append(ProviderStreamEvent(type='text_delta', text=text))
            return events
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                async with client.stream("POST", endpoint, params={"alt": "sse"},
                    headers={"x-goog-api-key": self._api_key},
                    json=_generation_payload(prompt, model_name, options)) as response:
                    if response.is_error:
                        error = TransientProviderError if response.status_code in {408, 429} or response.status_code >= 500 else PermanentProviderError
                        raise error("The streaming provider returned an error.", status_code=response.status_code)
                    lines = []
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            lines.append(line[5:].lstrip())
                        elif not line and lines:
                            events = frame('\n'.join(lines))
                            lines = []
                            for event in events:
                                yield event
                    if lines:
                        for event in frame('\n'.join(lines)):
                            yield event
        except httpx.RequestError as exc:
            raise TransientProviderError("The streaming provider connection failed.") from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise PermanentProviderError("The provider returned an invalid stream.") from exc
        yield ProviderStreamEvent(type="final", final=receipt())


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message")
        if isinstance(message, str) and message:
            return f"Gemini API returned HTTP {response.status_code}: {message}"
    except ValueError:
        pass
    return f"Gemini API returned HTTP {response.status_code}"


def _finish_reason(value: object) -> str:
    return {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "BLOCKLIST": "content_filter",
        "PROHIBITED_CONTENT": "content_filter",
    }.get(value, "unknown")


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None

def _valid_usage(usage):
    return all(_nonnegative_int(usage.get(key)) is not None for key in ('promptTokenCount', 'candidatesTokenCount'))

def _generation_payload(prompt, model_name, options):
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    config: dict[str, object] = {}
    if options is not None:
        if options.messages is not None:
            contents, instructions = [], []
            for message in options.messages:
                content = message.get("content")
                text = content if isinstance(content, str) else "\n".join(
                    part["text"] for part in content or [] if part.get("type") == "text")
                if message["role"] in {"system", "developer"}:
                    instructions.append({"text": text})
                elif message["role"] in {"user", "assistant"}:
                    contents.append({"role": "model" if message["role"] == "assistant" else "user",
                                     "parts": [{"text": text}]})
                else:
                    raise ProviderConfigurationError("Gemini tool history is unsupported by this profile")
            payload["contents"] = contents
            if instructions:
                payload["systemInstruction"] = {"parts": instructions}
        for source, target in (("temperature", "temperature"), ("top_p", "topP"), ("seed", "seed")):
            if (value := getattr(options, source)) is not None:
                config[target] = value
        if options.stop is not None:
            config["stopSequences"] = [options.stop] if isinstance(options.stop, str) else list(options.stop)
        if options.response_format and options.response_format["type"] in {"json_object", "json_schema"}:
            config["responseMimeType"] = "application/json"
            if options.response_format["type"] == "json_schema":
                config["responseJsonSchema"] = options.response_format["json_schema"]["schema"]
        if options.max_output_tokens is not None:
            # Gemini 2.5+ counts hidden reasoning against this cap.
            config["maxOutputTokens"] = (
                min(options.max_output_tokens, options.hard_completion_token_limit)
                if options.hard_completion_token_limit is not None
                else options.max_output_tokens + REASONING_TOKEN_HEADROOM
            )
        if options.json_schema is not None:
            config["responseFormat"] = {"text": {"mimeType": "application/json", "schema": options.json_schema}}
        if options.low_reasoning:
            if model_name.startswith("gemini-3"):
                config["thinkingConfig"] = {"thinkingLevel": "low"}
            elif model_name.startswith("gemini-2.5"):
                config["thinkingConfig"] = {"thinkingBudget": 1024}
    if config:
        payload["generationConfig"] = config
    return payload
