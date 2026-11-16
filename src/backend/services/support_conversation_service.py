from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import case, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.services.remote_support_synthesizer import RemoteSupportResponseSynthesizer
from src.backend.services.remote_support_understanding import RemoteSupportUnderstanding
from src.backend.services.support_chat_service import (
    SupportChatNotFoundError,
    SupportChatPermissionError,
    SupportChatService,
)
from src.backend.services.support_knowledge_service import normalize_support_text
from src.shared.config import get_settings
from src.shared.dependencies.auth import CurrentUser
from src.shared.dtos.support_chat import (
    SupportAnalyticsData,
    SupportChatContext,
    SupportChatMessage,
    SupportChatResponse,
    SupportChatSession,
    SupportChatSessionDetail,
    SupportChatSessionPage,
    SupportChatSessionUpdate,
    SupportConversationState,
)
from src.shared.models.core import Application
from src.shared.models.support_chat import (
    SupportChatMessageRecord,
    SupportChatRateLimitRecord,
    SupportChatSessionRecord,
)
from src.shared.services.support_safety import is_prompt_attack, redact_support_secrets, safe_support_history


class SupportChatRateLimitError(Exception):
    pass


class SupportConversationService:
    def __init__(
        self,
        session: AsyncSession,
        synthesizer: RemoteSupportResponseSynthesizer | None = None,
        understanding: RemoteSupportUnderstanding | None = None,
    ):
        self.session = session
        self.chat = SupportChatService(session)
        self.synthesizer = synthesizer or RemoteSupportResponseSynthesizer()
        self.understanding = understanding or RemoteSupportUnderstanding()

    @staticmethod
    def _authorize(current_user: CurrentUser) -> None:
        SupportChatService._ensure_chat_audience(current_user)
        SupportChatService._require_permission(current_user, "applications.read_own")

    async def create_session(self, current_user: CurrentUser) -> SupportChatSession:
        self._authorize(current_user)
        record = SupportChatSessionRecord(user_id=current_user.id)
        self.session.add(record)
        await self.session.commit()
        return self._session_dto(record)

    async def list_sessions(
        self, current_user: CurrentUser, *, offset: int = 0, limit: int = 30
    ) -> SupportChatSessionPage:
        self._authorize(current_user)
        rows = list(
            await self.session.scalars(
                select(SupportChatSessionRecord)
                .where(SupportChatSessionRecord.user_id == current_user.id)
                .order_by(
                    SupportChatSessionRecord.pinned.desc(),
                    SupportChatSessionRecord.updated_at.desc(),
                    SupportChatSessionRecord.id.desc(),
                )
                .offset(offset)
                .limit(limit + 1)
            )
        )
        return SupportChatSessionPage(
            items=[self._session_dto(row) for row in rows[:limit]],
            has_more=len(rows) > limit,
        )

    async def update_session(
        self,
        current_user: CurrentUser,
        session_id: str,
        update: SupportChatSessionUpdate,
    ) -> SupportChatSession:
        self._authorize(current_user)
        record = await self._owned_session(current_user.id, session_id)
        if update.title is not None:
            record.title = update.title
        if update.pinned is not None:
            record.pinned = update.pinned
        await self.session.commit()
        return self._session_dto(record)

    async def delete_session(self, current_user: CurrentUser, session_id: str) -> None:
        self._authorize(current_user)
        record = await self._owned_session(current_user.id, session_id)
        # Explicitly delete messages too, including deployments without FK cascades.
        await self.session.execute(
            delete(SupportChatMessageRecord).where(
                SupportChatMessageRecord.session_id == record.id,
                SupportChatMessageRecord.user_id == current_user.id,
            )
        )
        await self.session.delete(record)
        await self.session.commit()

    async def get_session(
        self, current_user: CurrentUser, session_id: str, *, offset: int = 0, limit: int = 100
    ) -> SupportChatSessionDetail:
        self._authorize(current_user)
        record = await self._owned_session(current_user.id, session_id)
        rows = list(
            await self.session.scalars(
                select(SupportChatMessageRecord)
                .where(
                    SupportChatMessageRecord.user_id == current_user.id,
                    SupportChatMessageRecord.session_id == record.id,
                )
                .order_by(SupportChatMessageRecord.created_at.desc(), SupportChatMessageRecord.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        )
        owned = await self._visible_application_ids(current_user.id, rows[:limit])
        return SupportChatSessionDetail(
            session=self._session_dto(record),
            messages=[self._message_dto(row, current_user, owned) for row in reversed(rows[:limit])],
            has_more=len(rows) > limit,
        )

    async def answer(
        self,
        *,
        question: str,
        current_user: CurrentUser,
        context: SupportChatContext | None,
        session_id: str | None,
    ) -> SupportChatResponse:
        self._authorize(current_user)
        record = await self._owned_session(current_user.id, session_id) if session_id is not None else None
        await self._enforce_rate_limit(current_user.id)
        # Validate browser resource hints before sharing any context with AI.
        if context and context.application_id:
            await self.chat._context_application(current_user.id, context)
        history = await self._recent_history(current_user, record) if record is not None else []
        try:
            state = (
                SupportConversationState.model_validate(record.state_json or {})
                if record
                else SupportConversationState()
            )
        except ValidationError:
            state = SupportConversationState()
        if state.application_id:
            owned_application = await self.session.scalar(
                select(Application.id).where(
                    Application.id == state.application_id,
                    Application.owner_user_id == current_user.id,
                )
            )
            if owned_application is None:
                state = SupportConversationState()
        raw_question = question
        question = redact_support_secrets(question)
        # Reject obvious attacks before any provider call. Refusals never enter model history.
        blocked = is_prompt_attack(raw_question) or self.chat._asks_for_internal_implementation(
            normalize_support_text(raw_question)
        )
        understanding = (
            None if blocked else await self.understanding.understand(question=question, state=state, history=history)
        )
        response = await self.chat.answer(
            question=raw_question if blocked else question,
            current_user=current_user,
            context=context,
            state=state,
            plan=understanding.plan if understanding else None,
            semantic_scores=understanding.semantic_scores if understanding else None,
            reranked_id=understanding.faq_id if understanding else None,
            faq_answer=understanding.faq_answer if understanding else None,
            clarification=understanding.clarification if understanding else None,
        )
        natural_message = response.message
        if not response.grounded_by_agent and not any(source.kind == "database" for source in response.sources):
            natural_message = await self.synthesizer.synthesize(
                question=question,
                response=response,
                history=history,
            )
        response = response.model_copy(update={"message": natural_message})
        if record is None:
            record = SupportChatSessionRecord(user_id=current_user.id)
            self.session.add(record)
            await self.session.flush()
        if response.conversation_state is not None and (
            response.status == "answered"
            or response.status == "needs_clarification"
        ):
            record.state_json = response.conversation_state.model_dump(mode="json")

        first_message = not await self.session.scalar(
            select(SupportChatMessageRecord.id)
            .where(
                SupportChatMessageRecord.user_id == current_user.id,
                SupportChatMessageRecord.session_id == record.id,
            )
            .limit(1)
        )
        if first_message:
            record.title = question.strip()[:120]

        now = datetime.now(UTC)
        record.updated_at = now
        self.session.add_all(
            [
                SupportChatMessageRecord(
                    session_id=record.id,
                    user_id=current_user.id,
                    role="user",
                    content=question,
                    created_at=now,
                ),
                SupportChatMessageRecord(
                    session_id=record.id,
                    user_id=current_user.id,
                    role="assistant",
                    **({"id": response.data.id} if response.data else {}),
                    data_json={
                        "result": response.data.model_dump(mode="json"),
                        "application_ids": response.data.application_ids,
                        "choices": [choice.model_dump(mode="json") for choice in response.choices],
                    }
                    if response.data
                    else {"choices": [choice.model_dump(mode="json") for choice in response.choices]}
                    if response.choices
                    else None,
                    content=response.message,
                    status=response.status,
                    required_permission=self._required_permission(response),
                    application_id=response.conversation_state.application_id
                    if response.conversation_state and any(source.kind == "database" for source in response.sources)
                    else None,
                    sources_json=[source.model_dump(mode="json") for source in response.sources],
                    actions_json=[action.model_dump(mode="json") for action in response.actions],
                    created_at=now + timedelta(microseconds=1),
                ),
            ]
        )
        await self.session.commit()
        return response.model_copy(
            update={
                "session_id": record.id,
                "suggestions": response.suggestions or SupportChatService.suggestions(current_user, context),
            }
        )

    @staticmethod
    def _record_data(record: SupportChatMessageRecord) -> SupportAnalyticsData | None:
        if not record.data_json:
            return None
        try:
            result = SupportAnalyticsData.model_validate(record.data_json["result"])
            result.application_ids = list(record.data_json.get("application_ids", []))
            return result
        except (KeyError, TypeError, ValidationError):
            return None

    async def chart_data(self, current_user: CurrentUser, chart_id: str) -> SupportAnalyticsData:
        self._authorize(current_user)
        SupportChatService._require_permission(current_user, "usage.read_own")
        record = await self.session.scalar(
            select(SupportChatMessageRecord).where(
                SupportChatMessageRecord.id == chart_id,
                SupportChatMessageRecord.user_id == current_user.id,
                SupportChatMessageRecord.role == "assistant",
            )
        )
        if record is None:
            raise SupportChatNotFoundError("Chart not found")
        owned = await self._visible_application_ids(current_user.id, [record])
        if not set((record.data_json or {}).get("application_ids", [])).issubset(owned):
            raise SupportChatNotFoundError("Chart not found")
        if record.required_permission and record.required_permission not in current_user.permissions:
            raise SupportChatPermissionError("Insufficient permission for chart")
        data = self._record_data(record)
        if data is None or data.id != record.id:
            raise SupportChatNotFoundError("Chart not found")
        return data

    async def _recent_history(
        self,
        current_user: CurrentUser,
        session: SupportChatSessionRecord,
    ) -> list[SupportChatMessage]:
        rows = list(
            await self.session.scalars(
                select(SupportChatMessageRecord)
                .where(
                    SupportChatMessageRecord.user_id == current_user.id,
                    SupportChatMessageRecord.session_id == session.id,
                )
                .order_by(SupportChatMessageRecord.created_at.desc(), SupportChatMessageRecord.id.desc())
                .limit(get_settings().support_chat_history_messages)
            )
        )
        owned = await self._visible_application_ids(current_user.id, rows)
        return safe_support_history(
            [self._message_dto(row, current_user, owned) for row in reversed(rows)],
            max_messages=get_settings().support_chat_history_messages,
            max_chars=get_settings().support_chat_context_chars,
        )

    async def _enforce_rate_limit(self, user_id: str) -> None:
        now = datetime.now(UTC)
        table = SupportChatRateLimitRecord.__table__
        stale = table.c.window_started_at <= now - timedelta(minutes=1)
        insert = postgres_insert if self.session.get_bind().dialect.name == "postgresql" else sqlite_insert
        statement = (
            insert(table)
            .values(user_id=user_id, window_started_at=now, request_count=1)
            .on_conflict_do_update(
                index_elements=[table.c.user_id],
                set_={
                    "window_started_at": case((stale, now), else_=table.c.window_started_at),
                    "request_count": case((stale, 1), else_=table.c.request_count + 1),
                },
                where=or_(stale, table.c.request_count < get_settings().support_chat_requests_per_minute),
            )
            .returning(table.c.user_id)
        )
        admitted = await self.session.scalar(statement)
        if admitted is None:
            await self.session.rollback()
            raise SupportChatRateLimitError("Support request limit reached")
        # Reserve before model calls; atomic across workers and counts failed attempts too.
        await self.session.commit()

    @staticmethod
    def _message_application_id(record: SupportChatMessageRecord) -> str | None:
        if record.application_id:
            return record.application_id
        for source in record.sources_json or []:
            for prefix in ("application:", "usage-summary:"):
                if source.get("id", "").startswith(prefix):
                    return source["id"].removeprefix(prefix)
        return None

    async def _visible_application_ids(self, user_id: str, rows: list[SupportChatMessageRecord]) -> set[str]:
        ids = {self._message_application_id(row) for row in rows} - {None}
        for row in rows:
            ids.update((row.data_json or {}).get("application_ids", []))
        if not ids:
            return set()
        return set(
            await self.session.scalars(
                select(Application.id).where(
                    Application.owner_user_id == user_id,
                    Application.id.in_(ids),
                )
            )
        )

    async def _owned_session(self, user_id: str, session_id: str) -> SupportChatSessionRecord:
        record = await self.session.scalar(
            select(SupportChatSessionRecord).where(
                SupportChatSessionRecord.id == session_id,
                SupportChatSessionRecord.user_id == user_id,
            )
        )
        if record is None:
            raise SupportChatNotFoundError("Chat session not found")
        return record

    @staticmethod
    def _required_permission(response: SupportChatResponse) -> str | None:
        source_ids = [source.id for source in response.sources if source.kind == "database"]
        if any(source_id.startswith("billing-account") for source_id in source_ids):
            return "billing.read"
        if any(source_id.startswith("usage-summary") for source_id in source_ids):
            return "usage.read_own"
        if "available-models" in source_ids:
            return "models.read_available"
        return None

    @staticmethod
    def _session_dto(record: SupportChatSessionRecord) -> SupportChatSession:
        return SupportChatSession(
            id=record.id,
            title=record.title,
            pinned=record.pinned,
            created_at=SupportChatService._timestamp(record.created_at),
            updated_at=SupportChatService._timestamp(record.updated_at),
        )

    @staticmethod
    def _message_dto(
        record: SupportChatMessageRecord, current_user: CurrentUser, owned: set[str] | None = None
    ) -> SupportChatMessage:
        application_id = SupportConversationService._message_application_id(record)
        data_scope = set((record.data_json or {}).get("application_ids", []))
        if (
            (owned is not None and not data_scope.issubset(owned))
            or (record.required_permission and record.required_permission not in current_user.permissions)
            or (application_id is not None and owned is not None and application_id not in owned)
        ):
            return SupportChatMessage(
                id=record.id,
                role="assistant",
                content="Nội dung này hiện không khả dụng theo quyền truy cập của bạn.",
                created_at=SupportChatService._timestamp(record.created_at),
            )
        return SupportChatMessage(
            id=record.id,
            role=record.role,
            content=record.content,
            created_at=SupportChatService._timestamp(record.created_at),
            status=record.status,
            sources=record.sources_json or [],
            actions=record.actions_json or [],
            choices=(record.data_json or {}).get("choices", []),
            data=SupportConversationService._record_data(record),
        )
