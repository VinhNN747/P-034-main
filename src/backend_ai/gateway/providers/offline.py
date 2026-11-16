from __future__ import annotations

from src.backend_ai.gateway.providers.base import (
    GenerationOptions,
    ProviderClient,
    ProviderResponse,
    ProviderStreamEvent,
)
from src.shared.dtos.gateway import OutputEvidence


class OfflineProvider(ProviderClient):
    """Deterministic local adapter used by tests and offline development."""

    name = "offline"

    async def stream(self, prompt, **kwargs):
        result = await self.generate(prompt, **kwargs)
        yield ProviderStreamEvent(type="text_delta", text=result.text)
        yield ProviderStreamEvent(type="final", final=result)

    async def generate(
        self,
        prompt: str,
        *,
        task_id: str,
        model_id: str,
        timeout_seconds: float,
        options: GenerationOptions | None = None,
    ) -> ProviderResponse:
        text = f"offline:{model_id}:{task_id}"
        return ProviderResponse(
            text=text,
            provider=self.name,
            model_id=model_id,
            input_tokens=max(1, len(prompt.split())),
            output_tokens=max(1, len(text.split())),
            latency_ms=0.0,
            metadata={
                "mode": "offline",
                "deterministic": True,
                "max_output_tokens": options.max_output_tokens if options else None,
                **({"openai_choices": [{"index": index, "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop"} for index in range(options.n)]}
                   if options and options.messages is not None else {}),
            },
            usage_received=True,
            output_evidence=OutputEvidence(finish_reason="stop"),
        )
