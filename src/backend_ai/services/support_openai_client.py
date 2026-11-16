"""Small OpenAI REST client for schema-bound support tasks and embeddings."""

import logging
import math
from collections import OrderedDict
from typing import TypeVar

import httpx
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)
_TEMPERATURE_SUPPORT: OrderedDict[tuple[str, str], bool] = OrderedDict()
_REASONING_SUPPORT: OrderedDict[tuple[str, str, str], bool] = OrderedDict()


class SupportOpenAIError(Exception):
    """Deliberately excludes provider bodies, credentials and submitted prompts."""

    def __init__(self, message: str, *, code: str | None = None, parameter: str | None = None):
        super().__init__(message)
        self.code = code
        self.parameter = parameter


class SupportOpenAIClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: float,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/"
        self.transport = transport

    async def _post(self, path: str, payload: dict) -> dict:
        if not self.api_key.strip():
            raise SupportOpenAIError("OpenAI support is not configured")
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(path, headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
                response.raise_for_status()
                body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Invalid provider response")
            return body
        except httpx.HTTPStatusError as exc:
            # Emit only allowlisted diagnostic identifiers; never a provider body
            # or message that may echo a user's input or credentials.
            category = "provider_error"
            parameter = "unknown"
            try:
                error = exc.response.json().get("error", {})
                if isinstance(error, dict):
                    error_code, error_parameter = error.get("code"), error.get("param")
                    if isinstance(error_code, str) and error_code in {
                        "model_not_found",
                        "unsupported_parameter",
                        "unsupported_value",
                        "invalid_api_key",
                        "insufficient_quota",
                        "rate_limit_exceeded",
                        "invalid_json_schema",
                    }:
                        category = error_code
                    if isinstance(error_parameter, str) and error_parameter in {
                        "temperature", "reasoning_effort", "model", "max_completion_tokens", "response_format",
                    }:
                        parameter = error_parameter
            except (ValueError, AttributeError):
                pass
            raise SupportOpenAIError(
                f"OpenAI support request failed: HTTP {exc.response.status_code}, {category}, parameter={parameter}",
                code=category,
                parameter=parameter,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SupportOpenAIError("OpenAI support request failed") from exc

    async def structured(
        self,
        *,
        system: str,
        data: str,
        schema: type[T],
        max_tokens: int = 1000,
        temperature: float = 0,
        reasoning_effort: str | None = None,
    ) -> T:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "store": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": data}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
        }
        cache_key = (self.base_url, self.model)
        reasoning_key = (*cache_key, reasoning_effort or "")
        if reasoning_effort and _REASONING_SUPPORT.get(reasoning_key) is not False:
            payload["reasoning_effort"] = reasoning_effort
            # Reasoning models may reject custom sampling parameters. Use the
            # provider default when explicitly asking the model to reason.
            payload.pop("temperature")
        if _TEMPERATURE_SUPPORT.get(cache_key) is False:
            payload.pop("temperature")
        for attempt in range(3):
            try:
                body = await self._post("chat/completions", payload)
                break
            except SupportOpenAIError as exc:
                if attempt == 2 or exc.code not in {"unsupported_parameter", "unsupported_value"} or exc.parameter not in {"temperature", "reasoning_effort"} or exc.parameter not in payload:
                    raise
                if exc.parameter == "reasoning_effort":
                    capability, key = _REASONING_SUPPORT, reasoning_key
                else:
                    capability, key = _TEMPERATURE_SUPPORT, cache_key
                capability[key] = False
                capability.move_to_end(key)
                while len(capability) > 128:
                    capability.popitem(last=False)
                logger.warning("Support provider rejected optional %s; using model default", exc.parameter)
                payload.pop(exc.parameter)
        try:
            choice = body["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") == "length":
                raise SupportOpenAIError(f"Structured support response exceeded completion budget: {schema.__name__}")
            if choice.get("finish_reason") != "stop" or message.get("refusal"):
                raise ValueError("Refused or incomplete structured response")
            return schema.model_validate_json(message["content"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise SupportOpenAIError("Invalid structured support response") from exc

    async def embeddings(self, texts: list[str], *, model: str) -> list[list[float]]:
        body = await self._post("embeddings", {"model": model, "input": texts, "encoding_format": "float"})
        try:
            rows = sorted(body["data"], key=lambda row: row["index"])
            if [row["index"] for row in rows] != list(range(len(texts))):
                raise ValueError("Missing embedding")
            vectors = [[float(value) for value in row["embedding"]] for row in rows]
            if not vectors or not vectors[0] or any(len(v) != len(vectors[0]) for v in vectors):
                raise ValueError("Invalid embedding dimensions")
            if any(not all(math.isfinite(x) for x in v) or not any(v) for v in vectors):
                raise ValueError("Invalid embedding values")
            return vectors
        except (KeyError, TypeError, ValueError) as exc:
            raise SupportOpenAIError("Invalid support embedding response") from exc
