import logging
from collections.abc import Sequence

from pydantic import ValidationError

from src.backend.clients.ai_backend_client import AIBackendClient, AIBackendUnavailableError, get_ai_backend_client
from src.shared.dtos.ai_internal import SupportSynthesisInput, SupportSynthesisOutput
from src.shared.dtos.support_chat import SupportChatMessage, SupportChatResponse
from src.shared.services.support_safety import redact_support_secrets, safe_support_history, validate_support_rewrite

logger = logging.getLogger(__name__)


class RemoteSupportResponseSynthesizer:
    def __init__(self, client: AIBackendClient | None = None):
        self.client = client or get_ai_backend_client()

    async def synthesize(
        self, *, question: str, response: SupportChatResponse, history: Sequence[SupportChatMessage]
    ) -> str:
        if (
            not self.client.settings.support_chat_llm_enabled
            or response.status not in {"answered", "needs_clarification"}
            or any(source.kind == "database" for source in response.sources)
        ):
            return response.message
        try:
            settings = self.client.settings
            payload = SupportSynthesisInput(
                question=redact_support_secrets(question),
                response=response,
                history=safe_support_history(
                    history,
                    max_messages=settings.support_chat_history_messages,
                    max_chars=settings.support_chat_context_chars,
                ),
            )
            body = await self.client.post("/internal/v1/support/synthesize", payload.model_dump(mode="json"))
            message = SupportSynthesisOutput.model_validate(body).message
            return (
                message
                if validate_support_rewrite(message, response, [s.id for s in response.sources])
                else response.message
            )
        except AIBackendUnavailableError as exc:
            if exc.status_code == 404:
                logger.error(
                    "AI synthesis endpoint was not found; verify AI_BACKEND_URL points to src.backend_ai.main:app"
                )
            else:
                logger.warning("AI support synthesis unavailable; using verified response")
            return response.message
        except ValidationError:
            logger.warning("AI support synthesis unavailable; using verified response")
            return response.message
