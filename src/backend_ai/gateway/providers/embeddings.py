from __future__ import annotations

import base64
import math
import struct
from dataclasses import dataclass
from time import perf_counter
from typing import Literal

from src.backend_ai.dtos.openai_embeddings import EmbeddingData, EmbeddingRequest, EmbeddingResponse, EmbeddingUsage
from src.backend_ai.services.embedding_validation import ValidatedEmbeddingInput


class EmbeddingProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = "embedding_provider_error",
        retryable: bool = False,
        retry_after: float | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class EmbeddingProviderResult:
    model: str
    vectors: list[list[float]]
    input_tokens: int
    latency_ms: float


class OpenAIEmbeddingProvider:
    """Direct OpenAI embedding adapter; never routes through the chat pipeline."""

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def probe(self, model_name: str, *, timeout_seconds: float, input_text: str = "health probe") -> None:
        """Issue one minimal embedding request to verify this connection/model pair."""
        from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

        kwargs = {"api_key": self.api_key, "timeout": timeout_seconds, "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        try:
            async with AsyncOpenAI(**kwargs) as client:
                await client.embeddings.create(model=model_name, input=input_text)
        except APIStatusError as exc:
            raise EmbeddingProviderError(
                "The embedding provider returned an error.",
                status_code=exc.status_code,
                code="embedding_provider_error",
            ) from exc
        except APITimeoutError as exc:
            raise EmbeddingProviderError(
                "The embedding provider request timed out.", status_code=408, code="embedding_timeout"
            ) from exc
        except APIConnectionError as exc:
            raise EmbeddingProviderError(
                "The embedding provider connection failed.", status_code=0, code="embedding_network_error"
            ) from exc
        except Exception as exc:
            raise EmbeddingProviderError("The embedding provider request failed.") from exc

    async def embed(
        self,
        payload: EmbeddingRequest,
        validated: ValidatedEmbeddingInput,
        *,
        timeout_seconds: float,
    ) -> EmbeddingProviderResult:
        from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

        kwargs = {"api_key": self.api_key, "timeout": timeout_seconds, "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        started = perf_counter()
        try:
            async with AsyncOpenAI(**kwargs) as client:
                request_args = {
                    "model": payload.model,
                    "input": validated.provider_input,
                    "encoding_format": "float",
                }
                if payload.dimensions is not None:
                    request_args["dimensions"] = validated.expected_dimensions
                if payload.user is not None:
                    request_args["user"] = payload.user
                response = await client.embeddings.create(**request_args)
        except APIStatusError as exc:
            retry_after = _retry_after(exc)
            retryable = exc.status_code in {408, 429} or exc.status_code >= 500
            raise EmbeddingProviderError(
                "The embedding provider returned an error.",
                status_code=exc.status_code,
                code="embedding_provider_error",
                retryable=retryable,
                retry_after=retry_after,
            ) from exc
        except APITimeoutError as exc:
            raise EmbeddingProviderError(
                "The embedding provider request timed out.",
                status_code=503,
                code="operation_outcome_unknown",
                outcome_unknown=True,
            ) from exc
        except APIConnectionError as exc:
            raise EmbeddingProviderError(
                "The embedding provider connection failed.",
                status_code=503,
                code="operation_outcome_unknown",
                outcome_unknown=True,
            ) from exc
        except Exception as exc:
            # Do not put upstream exception text (which can contain URLs or
            # request fragments) in a user-visible error.
            raise EmbeddingProviderError("The embedding provider request failed.") from exc

        vectors = _validate_response(response, payload, validated)
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if type(input_tokens) is not int or input_tokens <= 0 or type(total_tokens) is not int:
            raise _invalid_provider_response("Provider usage is missing or invalid.")
        if total_tokens != input_tokens:
            raise _invalid_provider_response("Provider embedding usage totals are inconsistent.")
        return EmbeddingProviderResult(
            model=payload.model,
            vectors=vectors,
            input_tokens=input_tokens,
            latency_ms=(perf_counter() - started) * 1000,
        )


def _validate_response(response, payload: EmbeddingRequest, validated: ValidatedEmbeddingInput) -> list[list[float]]:
    if getattr(response, "model", None) != payload.model:
        raise _invalid_provider_response("Provider returned a different model identity.")
    data = getattr(response, "data", None)
    if not isinstance(data, list) or len(data) != validated.input_count:
        raise _invalid_provider_response("Provider returned an unexpected number of vectors.")
    ordered: list[list[float] | None] = [None] * validated.input_count
    for item in data:
        index = getattr(item, "index", None)
        vector = getattr(item, "embedding", None)
        if type(index) is not int or not 0 <= index < validated.input_count or ordered[index] is not None:
            raise _invalid_provider_response("Provider returned invalid vector indices.")
        if not isinstance(vector, list) or len(vector) != validated.expected_dimensions:
            raise _invalid_provider_response("Provider returned a vector with an unexpected dimension.")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise _invalid_provider_response("Provider returned a non-finite or invalid vector value.")
        try:
            packed = struct.pack(f"<{len(vector)}f", *(float(value) for value in vector))
            normalized = list(struct.unpack(f"<{len(vector)}f", packed))
        except (OverflowError, struct.error) as exc:
            raise _invalid_provider_response("Provider returned an out-of-range vector value.") from exc
        if any(not math.isfinite(value) for value in normalized):
            raise _invalid_provider_response("Provider returned a non-finite vector value.")
        ordered[index] = normalized
    if any(vector is None for vector in ordered):
        raise _invalid_provider_response("Provider omitted a vector index.")
    return [vector for vector in ordered if vector is not None]


def _invalid_provider_response(message: str) -> EmbeddingProviderError:
    return EmbeddingProviderError(message, status_code=502, code="invalid_provider_response")


def _retry_after(exc) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    value = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def serialize_embeddings(
    result: EmbeddingProviderResult,
    encoding: Literal["float", "base64"],
) -> EmbeddingResponse:
    data: list[EmbeddingData] = []
    for index, vector in enumerate(result.vectors):
        packed = struct.pack(f"<{len(vector)}f", *vector)
        embedding: list[float] | str
        if encoding == "base64":
            embedding = base64.b64encode(packed).decode("ascii")
        else:
            embedding = list(struct.unpack(f"<{len(vector)}f", packed))
        data.append(EmbeddingData(index=index, embedding=embedding))
    return EmbeddingResponse(
        data=data,
        model=result.model,
        usage=EmbeddingUsage(prompt_tokens=result.input_tokens, total_tokens=result.input_tokens),
    )
