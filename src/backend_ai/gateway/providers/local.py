from __future__ import annotations

from src.backend_ai.gateway.providers.base import PermanentProviderError, ProviderConfigurationError
from src.backend_ai.gateway.providers.openai import OpenAIProvider
from src.shared.services.local_endpoints import LocalEndpointError, ensure_endpoint_reachable

# vLLM without --api-key, Ollama, LM Studio and llama.cpp accept any bearer token,
# but the OpenAI SDK refuses to send a request without one.
_NO_KEY = "local-no-key"


class LocalProvider(OpenAIProvider):
    """A self-hosted model server through its OpenAI-compatible ``/v1`` chat completions API.

    Works with vLLM (``vllm serve <model>``, default ``http://localhost:8000/v1``) and Ollama
    (``http://localhost:11434/v1``). The catalog model name is sent as is, so it must match the
    name the server serves (``Qwen/Qwen2.5-1.5B-Instruct`` on vLLM, ``qwen2.5:1.5b`` on Ollama).
    ``reasoning_effort`` is OpenAI-specific and is not sent, and no reasoning headroom is added:
    small local models have short context windows.

    With ``allow_private=False`` (a customer's endpoint on a hosted gateway) the host is resolved
    before every call and refused unless every address is public, and redirects are not followed,
    so the endpoint cannot be turned into a path into the gateway's own network.
    """

    name = "local"
    sends_reasoning_effort = False
    uses_max_tokens_field = True

    def __init__(self, *, base_url: str, api_key: str | None = None, allow_private: bool = True):
        if not base_url:
            raise ProviderConfigurationError("A local provider needs the server's base URL")
        super().__init__(enabled=True, base_url=base_url, api_key=api_key or _NO_KEY)
        self._allow_private = allow_private

    def _is_reasoning(self, model_id: str) -> bool:
        return False

    def _http_client(self):
        if self._allow_private:
            return None
        from openai import DefaultAsyncHttpxClient

        return DefaultAsyncHttpxClient(follow_redirects=False)

    async def _guard(self) -> None:
        try:
            await ensure_endpoint_reachable(self._base_url, allow_private=self._allow_private)
        except LocalEndpointError as exc:
            raise PermanentProviderError(str(exc)) from exc

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        await self._guard()
        return await super().generate(
            prompt, task_id=task_id, model_id=model_id, timeout_seconds=timeout_seconds, options=options
        )

    async def stream(self, prompt, *, task_id, model_id, timeout_seconds, options=None):
        await self._guard()
        async for event in super().stream(
            prompt, task_id=task_id, model_id=model_id, timeout_seconds=timeout_seconds, options=options
        ):
            yield event
