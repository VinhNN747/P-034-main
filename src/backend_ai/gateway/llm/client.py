from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

from src.shared.dtos.gateway import ModelUsage, OutputEvidence, StructuredModelResponse

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class StructuredModelClient(ABC):
    """Provider-neutral interface used by LLM stages and easily mocked in tests."""

    @abstractmethod
    async def generate(self, prompt: str, schema: type[SchemaT], model: str, timeout: float) -> StructuredModelResponse:
        raise NotImplementedError

    async def generate_output(self, prompt, model, timeout, *, options):
        raise NotImplementedError("This client has no public final-output producer")


class LangChainOpenAIClient(StructuredModelClient):
    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url

    def _make_llm(self, model: str, timeout: float):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=0,
            timeout=timeout,
            max_retries=0,
        )

    async def generate_output(self, prompt, model, timeout, *, options):
        from src.backend_ai.gateway.providers.openai import OpenAIProvider
        from src.backend_ai.services.runtime_stream_service import current_stream
        provider = OpenAIProvider(enabled=True, api_key=self.api_key, base_url=self.base_url)
        kwargs = dict(task_id="aggregate", model_id=model, timeout_seconds=timeout, options=options)
        stream = current_stream()
        if stream is None:
            return await provider.generate(prompt, **kwargs)
        stream.model = model
        final = None
        async for event in provider.stream(prompt, **kwargs):
            if event.final is not None:
                stream.record_usage(event.final, task_id='aggregate')
            if event.type == "final":
                final = event.final
            else:
                await stream.emit(event)
        if final is None:
            raise RuntimeError("Aggregation stream ended without a final receipt")
        return final

    async def generate(self, prompt: str, schema: type[SchemaT], model: str, timeout: float) -> StructuredModelResponse:
        if not self.api_key:
            raise RuntimeError("The OpenAI provider credential is not available")
        llm = self._make_llm(model, timeout)
        runnable = llm.with_structured_output(schema, include_raw=True)
        result = await runnable.ainvoke(prompt)
        parsed = result.get("parsed")
        if parsed is None:
            raise ValueError(f"Structured output was not parsed: {result.get('parsing_error')}")
        raw = result.get("raw")
        usage_data = getattr(raw, "usage_metadata", None) or {}
        response_metadata = getattr(raw, "response_metadata", None) or {}
        token_usage = response_metadata.get("token_usage", {}) if isinstance(response_metadata, dict) else {}
        input_details = usage_data.get("input_token_details", {}) if isinstance(usage_data, dict) else {}
        output_details = usage_data.get("output_token_details", {}) if isinstance(usage_data, dict) else {}
        prompt_details = token_usage.get("prompt_tokens_details", {}) if isinstance(token_usage, dict) else {}
        completion_details = token_usage.get("completion_tokens_details", {}) if isinstance(token_usage, dict) else {}
        finish_reason = response_metadata.get("finish_reason") or response_metadata.get("stop_reason") if isinstance(response_metadata, dict) else None
        finish_reason = {
            "stop": "stop", "end_turn": "stop", "length": "length", "max_tokens": "length",
            "content_filter": "content_filter", "safety": "content_filter",
        }.get(str(finish_reason).lower(), "unknown")

        def measured_counter(*values):
            return next((value for value in values if type(value) is int and value >= 0), None)

        usage = ModelUsage(
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
        )
        return StructuredModelResponse(
            data=parsed.model_dump(mode="json", exclude_none=True),
            usage=usage,
            model=model,
            usage_complete=isinstance(usage_data, dict)
            and any(key in usage_data for key in ("input_tokens", "output_tokens")),
            output_evidence=OutputEvidence(
                finish_reason=finish_reason,
                cached_tokens=measured_counter(prompt_details.get("cached_tokens"), input_details.get("cache_read")),
                cache_write_tokens=measured_counter(
                    prompt_details.get("cache_write_tokens"), input_details.get("cache_creation")
                ),
                reasoning_tokens=measured_counter(
                    completion_details.get("reasoning_tokens"), output_details.get("reasoning")
                ),
            ),
        )
