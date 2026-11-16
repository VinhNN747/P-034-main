from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from src.shared.dtos.gateway import OutputEvidence


@dataclass(frozen=True)
class GenerationOptions:
    max_output_tokens: int | None = None
    hard_completion_token_limit: int | None = None
    json_schema: dict[str, Any] | None = None
    low_reasoning: bool = False
    messages: tuple[dict[str, Any], ...] | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    tools: tuple[dict[str, Any], ...] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    n: int = 1


@dataclass(frozen=True)
class ProviderResponse:
    """Normalized result returned by every model provider adapter."""

    text: str
    provider: str
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    usage_received: bool = False
    output_evidence: OutputEvidence = field(default_factory=OutputEvidence)


@dataclass(frozen=True)
class ProviderStreamEvent:
    type: str
    text: str | None = None
    final: ProviderResponse | None = None
    index: int = 0
    tool_delta: dict[str, Any] | None = None


# Thinking models (OpenAI gpt-5/o-series, Gemini 2.5+) count hidden reasoning
# tokens against the output limit. With only the application's answer budget
# (default 800) a hard prompt can spend all of it thinking and return no text,
# so providers add this headroom for them. Billing still uses real usage.
REASONING_TOKEN_HEADROOM = 4096


class ProviderError(Exception):
    transient = False

    def __init__(self, *args: object, retry_after: float | None = None, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.retry_after = retry_after
        self.status_code = status_code


class TransientProviderError(ProviderError):
    transient = True


class PermanentProviderError(ProviderError):
    transient = False


class ProviderConfigurationError(PermanentProviderError):
    pass


class ProviderOutputError(PermanentProviderError):
    """An unusable model output with its observed, potentially billable receipt."""

    def __init__(self, code: str, message: str, response: ProviderResponse):
        super().__init__(message)
        self.code = code
        self.response = response


class ModelUnavailableError(PermanentProviderError):
    """This model cannot serve the request (it does not exist for the key, or
    it returned no visible output), but another candidate model can: the
    executor moves on to the next model instead of retrying or stopping."""


class ProviderClient(ABC):
    name = "provider"

    async def stream(self, prompt: str, *, task_id: str, model_id: str, timeout_seconds: float,
                     options: GenerationOptions | None = None) -> AsyncIterator[ProviderStreamEvent]:
        raise ProviderConfigurationError("This provider does not support streaming")
        yield  # pragma: no cover - async iterator interface

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        task_id: str,
        model_id: str,
        timeout_seconds: float,
        options: GenerationOptions | None = None,
    ) -> ProviderResponse:
        raise NotImplementedError
