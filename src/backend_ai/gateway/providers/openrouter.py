from __future__ import annotations

from src.backend_ai.gateway.providers.base import ProviderConfigurationError
from src.backend_ai.gateway.providers.openai import OpenAIProvider

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_CATALOG_PREFIX = "openrouter/"


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter through its OpenAI-compatible chat completions API.

    Catalog rows are named LiteLLM-style ("openrouter/deepseek/deepseek-v4-pro");
    OpenRouter itself expects "deepseek/deepseek-v4-pro", so the prefix is
    stripped. ``reasoning_effort`` is OpenAI-specific and is not sent: models
    behind OpenRouter use their own default reasoning. Every model gets the
    reasoning token headroom, since many of them think before answering and
    the headroom is only a cap.
    """

    name = "openrouter"
    sends_reasoning_effort = False

    def __init__(self, *, api_key: str | None = None, enabled: bool = True, base_url: str | None = None):
        try:
            super().__init__(
                api_key=api_key,
                enabled=enabled,
                base_url=base_url or OPENROUTER_BASE_URL,
            )
        except ProviderConfigurationError as exc:
            raise ProviderConfigurationError(str(exc).replace("OpenAI", "OpenRouter")) from exc

    def _upstream_model(self, model_id: str) -> str:
        return super()._upstream_model(model_id).removeprefix(_CATALOG_PREFIX)

    def _is_reasoning(self, model_id: str) -> bool:
        return True
