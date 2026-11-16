"""Management's HTTP boundary to the independently deployed AI service."""

from typing import Any

import httpx

from src.shared.config import Settings, get_settings


class AIBackendUnavailableError(Exception):
    def __init__(self, status_code: int | None = None):
        super().__init__("AI service is unavailable.")
        self.status_code = status_code


class AIBackendClient:
    def __init__(self, settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        self.transport = transport

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.ai_service_token:
            raise AIBackendUnavailableError()
        headers = {"X-AI-Service-Token": self.settings.ai_service_token}
        if actor_user_id:
            headers["X-Actor-User-ID"] = actor_user_id
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.ai_backend_url.rstrip("/"),
                timeout=max(self.settings.ai_backend_timeout_seconds, self.settings.support_chat_llm_timeout_seconds + 10)
                if path.startswith("/internal/v1/support/") else self.settings.ai_backend_timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    path, json=payload, headers=headers,
                )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Invalid AI response")
            return body
        except httpx.HTTPStatusError as exc:
            raise AIBackendUnavailableError(status_code=exc.response.status_code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AIBackendUnavailableError() from exc


def get_ai_backend_client() -> AIBackendClient:
    return AIBackendClient()
