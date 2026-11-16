import logging
import math

from pydantic import ValidationError

from src.backend.clients.ai_backend_client import AIBackendClient, AIBackendUnavailableError, get_ai_backend_client
from src.backend.services.support_knowledge_service import SupportKnowledgeService
from src.shared.dtos.ai_internal import SupportUnderstandingInput, SupportUnderstandingOutput
from src.shared.dtos.support_chat import SupportChatMessage, SupportConversationState
from src.shared.services.support_safety import (
    redact_support_secrets,
    safe_support_history,
    validate_support_clarification,
    validate_support_faq_answer,
)

logger = logging.getLogger(__name__)


class RemoteSupportUnderstanding:
    def __init__(self, client: AIBackendClient | None = None):
        self.client = client or get_ai_backend_client()

    async def understand(
        self,
        *,
        question: str,
        state: SupportConversationState,
        history: list[SupportChatMessage],
    ) -> SupportUnderstandingOutput:
        if not self.client.settings.support_chat_llm_enabled:
            return SupportUnderstandingOutput()
        settings = self.client.settings
        payload = SupportUnderstandingInput(
            question=redact_support_secrets(question),
            previous_intent=state.intent,
            pending_slot=state.pending_slot,
            previous_period=state.period,
            previous_analytics=state.analytics,
            previous_application_name=state.application_name,
            history=safe_support_history(
                history,
                max_messages=settings.support_chat_history_messages,
                max_chars=settings.support_chat_context_chars,
            ),
            documents=SupportKnowledgeService().documents(question),
        )
        try:
            body = await self.client.post("/internal/v1/support/understand", payload.model_dump(mode="json"))
            result = SupportUnderstandingOutput.model_validate(body)
            known = {doc.id for doc in payload.documents}
            if result.clarification is not None and (
                not result.clarification.needed or result.plan is None or result.plan.intent != "clarify"
                or not validate_support_clarification(result.clarification.message, result.clarification.choices)
            ):
                raise ValueError("Invalid clarification")
            if result.faq_id is not None and result.faq_id not in known:
                raise ValueError("Invalid selected FAQ")
            if result.faq_answer is not None and not validate_support_faq_answer(
                result.faq_answer.message, result.faq_answer.source_ids, {doc.id: doc.text for doc in payload.documents}
            ):
                raise ValueError("Invalid grounded FAQ answer")
            if any(
                key not in known or not math.isfinite(score) or not -1 <= score <= 1
                for key, score in result.semantic_scores.items()
            ):
                raise ValueError("Invalid retrieval scores")
            return result
        except AIBackendUnavailableError as exc:
            if exc.status_code == 404:
                logger.error(
                    "AI support endpoint was not found; verify AI_BACKEND_URL points to src.backend_ai.main:app"
                )
            else:
                logger.warning("Support understanding unavailable; using BM25 and deterministic intent fallback")
            return SupportUnderstandingOutput()
        except (ValidationError, ValueError):
            logger.warning("Support understanding unavailable; using BM25 and deterministic intent fallback")
            return SupportUnderstandingOutput()
