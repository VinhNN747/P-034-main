from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.services.support_data_service import SupportDataQueryError, SupportDataService
from src.backend.services.support_intent_service import explicit_support_period, fallback_support_intent
from src.backend.services.support_knowledge_service import (
    SupportArticle,
    SupportKnowledgeService,
    is_assistant_capability_question,
    is_assistant_model_question,
    normalize_support_text,
    prefers_vietnamese,
)
from src.shared.config import get_settings
from src.shared.dependencies.auth import CurrentUser
from src.shared.dtos.ai_internal import SupportClarificationDecision, SupportFAQAnswer
from src.shared.dtos.support_chat import (
    SupportChatAction,
    SupportChatChoice,
    SupportChatContext,
    SupportChatResponse,
    SupportChatSource,
    SupportConversationState,
    SupportIntentPlan,
)
from src.shared.models.billing import BillingAccountRecord
from src.shared.models.core import Application
from src.shared.models.gateway import ApplicationGatewayConfig, Model, Provider
from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord
from src.shared.repositories.routing_catalog import visible_to
from src.shared.services.support_navigation import SUPPORT_NAVIGATION
from src.shared.services.support_safety import is_prompt_attack, validate_support_clarification
from src.shared.services.taskway_documentation import documentation_links


class SupportChatAccessError(Exception):
    pass


class SupportChatPermissionError(Exception):
    pass


class SupportChatNotFoundError(Exception):
    pass


class SupportChatService:
    """Grounded support answers with an explicit, read-only tool allowlist."""

    def __init__(self, session: AsyncSession, knowledge: SupportKnowledgeService | None = None):
        self.session = session
        self.knowledge = knowledge or SupportKnowledgeService()

    async def answer(
        self,
        *,
        question: str,
        current_user: CurrentUser,
        context: SupportChatContext | None = None,
        state: SupportConversationState | None = None,
        plan: SupportIntentPlan | None = None,
        semantic_scores: dict[str, float] | None = None,
        reranked_id: str | None = None,
        faq_answer: SupportFAQAnswer | None = None,
        clarification: SupportClarificationDecision | None = None,
    ) -> SupportChatResponse:
        self._ensure_chat_audience(current_user)
        self._require_permission(current_user, "applications.read_own")
        normalized_question = normalize_support_text(question)
        if is_prompt_attack(question) or self._asks_for_internal_implementation(normalized_question):
            if prefers_vietnamese(question):
                message = (
                    "Tôi chỉ có thể hỗ trợ cách sử dụng Gateway và dữ liệu workspace mà bạn được phép xem. "
                    "Tôi không cung cấp mã nguồn, prompt nội bộ, secret hoặc dữ liệu của tài khoản khác; "
                    "chỉ dẫn trong hội thoại cũng không thể thay đổi quyền truy cập của bạn."
                )
            else:
                message = (
                    "I can only help with Gateway usage and workspace data you are authorized to view. "
                    "I cannot provide source code, internal prompts, secrets, or another account's data, "
                    "and conversation instructions cannot change your access permissions."
                )
            return self._response(
                status="insufficient_evidence",
                message=message,
            )
        previous = state or SupportConversationState()
        requested_days = re.search(r"\b(\d+)\s*(?:ngay|days)\b", normalized_question)
        unsupported_days = bool(requested_days and int(requested_days.group(1)) not in {7, 30})
        local_plan = fallback_support_intent(question, previous)
        resolved = plan or local_plan
        # Resolve server-owned clarification slots before selecting an answer tool.
        # Names are matched only against currently owned applications.
        if previous.pending_slot == "application" and resolved.intent != "blocked":
            selected = await self._named_application(current_user.id, normalized_question)
            if selected is not None and (
                explicit_support_period(question) is not None or len(normalized_question.split()) <= 6
            ):
                resolved = SupportIntentPlan(
                    intent=previous.intent,
                    confidence=1,
                    application_name=None,
                    period=explicit_support_period(question),
                    follow_up=True,
                    analytics=previous.analytics,
                )
        # A short period selection is deterministic conversation context. Prefer it
        # when a remote classifier is uncertain instead of dropping the prior topic.
        if (
            plan is not None
            and previous.pending_slot == "period"
            and resolved.intent != "blocked"
            and local_plan.follow_up
            and explicit_support_period(question) is not None
            and (
                resolved.intent in {"faq", "clarify"}
                or resolved.confidence < get_settings().support_chat_intent_threshold
            )
        ):
            resolved = local_plan
        # Preserve the underlying supported topic while asking the user to choose a
        # supported rolling window. This state is persisted for the next turn.
        if (
            unsupported_days
            and resolved.intent in {"faq", "clarify"}
            and local_plan.intent not in {"faq", "clarify"}
        ):
            resolved = local_plan
        if resolved.intent == "blocked":
            return self._response(
                status="insufficient_evidence",
                message="I cannot change system rules, reveal internal information, or access data outside your permissions.",
            )
        if is_assistant_model_question(question):
            resolved = resolved.model_copy(
                update={"intent": "faq", "confidence": 1, "application_name": None, "follow_up": False}
            )
        if faq_answer is not None and resolved.intent in {"faq", "routing", "clarify"}:
            resolved = resolved.model_copy(update={"intent": "faq", "confidence": 1})
        if resolved.intent == "clarify" and clarification is not None and clarification.needed and validate_support_clarification(clarification.message, clarification.choices):
            return self._response(status="needs_clarification", message=clarification.message).model_copy(update={
                "choices": clarification.choices, "grounded_by_agent": True,
                "conversation_state": SupportConversationState(intent="clarify"),
            })
        if plan is not None and resolved.confidence < get_settings().support_chat_intent_threshold:
            return self._clarification()
        if unsupported_days and resolved.intent in {"usage", "analytics", "application_statistics"}:
            pending_state = previous.model_copy(deep=True)
            pending_state.intent = resolved.intent
            pending_state.faq_id = None
            pending_state.analytics = resolved.analytics if resolved.intent == "analytics" else None
            pending_state.pending_slot = "period"
            if resolved.intent in {"usage", "analytics", "application_statistics"}:
                self._require_permission(current_user, "usage.read_own")
            selected = await self._context_application(current_user.id, context)
            selected = await self._named_application(current_user.id, normalized_question) or selected
            if selected is not None:
                pending_state.application_id = selected.id
                pending_state.application_name = selected.name
            return self._response(
                status="needs_clarification",
                message=(
                    "I understand what you want to review, but rolling periods currently support 7 or 30 days. "
                    "Choose one of those options, or provide a YYYY-MM-DD date range of up to 90 days; the end date is exclusive."
                ),
            ).model_copy(
                update={
                    "conversation_state": pending_state,
                    "choices": [
                        SupportChatChoice(label="Last 7 days", message="Use the last 7 days"),
                        SupportChatChoice(label="Last 30 days", message="Use the last 30 days"),
                    ],
                }
            )
        if resolved.intent == "analytics":
            query = resolved.analytics
            if query is None:
                return self._clarification()
            # Dates are evidence extracted from the question, never invented by a planner.
            if any(
                value
                and value not in question
                and not (
                    resolved.follow_up
                    and previous.analytics
                    and value in {previous.analytics.start_date, previous.analytics.end_date}
                )
                for value in (query.start_date, query.end_date)
            ):
                return self._clarification()
            wants_chart = self._matches(normalized_question, "bieu do", "chart", "plot", "graph", "bien dong", "trend")
            if not wants_chart and not (resolved.follow_up and previous.analytics and previous.analytics.chart_type):
                query = query.model_copy(update={"chart_type": None})
            resolved = resolved.model_copy(update={"analytics": query})
        application = await self._context_application(current_user.id, context)
        next_state = previous.model_copy(deep=True)
        next_state.intent = resolved.intent
        next_state.period = (
            explicit_support_period(question)
            or resolved.period
            or (previous.period if resolved.follow_up else "last_7_days")
        )
        next_state.faq_id = None
        next_state.pending_slot = None
        next_state.analytics = resolved.analytics if resolved.intent == "analytics" else None
        if application is None and resolved.follow_up and previous.application_id:
            try:
                application = await self._context_application(
                    current_user.id, SupportChatContext(application_id=previous.application_id)
                )
            except SupportChatNotFoundError:
                return self._response(
                    status="insufficient_evidence",
                    message="The application from the previous context is no longer available. Choose an application you are allowed to view.",
                )
        if resolved.intent in {"application_detail", "application_statistics", "usage", "analytics"}:
            permission = "usage.read_own" if resolved.intent != "application_detail" else "applications.read_own"
            self._require_permission(current_user, permission)
            # Model-proposed names must occur in this question; model IDs are never accepted.
            proposed_name = normalize_support_text(resolved.application_name or "")
            if proposed_name and not self._contains_name(normalized_question, proposed_name):
                if (
                    resolved.follow_up
                    and previous.application_id
                    and proposed_name == normalize_support_text(previous.application_name or "")
                ):
                    # Ignore a model's echoed old name; only the re-authorized server slot selects the app.
                    proposed_name = ""
                else:
                    return self._clarification()
            named = await self._named_application(current_user.id, proposed_name or normalized_question)
            if named is not None:
                application = named
            elif proposed_name or self._has_explicit_application_name(normalized_question):
                return self._response(
                    status="insufficient_evidence",
                    message="I could not find that application in your account. Choose one of your applications to view its data.",
                )
            if application is None and resolved.intent not in {"usage", "analytics"}:
                next_state.application_id = None
                next_state.application_name = None
                next_state.pending_slot = "application"
                application = await self._single_application(current_user.id)
            if application is None and resolved.intent not in {"usage", "analytics"}:
                response = await self._applications_answer(
                    current_user, introduction="Choose one of your applications to view its data. "
                )
                return response.model_copy(
                    update={"status": "needs_clarification", "conversation_state": next_state}
                )
        if (
            resolved.intent in {"usage", "analytics"}
            and not resolved.application_name
            and self._matches(normalized_question, "tat ca", "toan bo", "all apps", "all applications")
        ):
            application = None
        next_state.application_id = application.id if application else None
        next_state.application_name = application.name if application else None
        response = await self._answer_intent(
            question=question,
            current_user=current_user,
            application=application,
            state=next_state,
            semantic_scores=semantic_scores,
            reranked_id=reranked_id,
            faq_answer=faq_answer if resolved.intent == "faq" else None,
            agent_planned=plan is not None and not is_assistant_model_question(question),
        )
        return response.model_copy(update={"conversation_state": next_state})

    async def _answer_intent(
        self,
        *,
        question: str,
        current_user: CurrentUser,
        application: Application | None,
        state: SupportConversationState,
        semantic_scores: dict[str, float] | None,
        reranked_id: str | None,
        faq_answer: SupportFAQAnswer | None,
        agent_planned: bool,
    ) -> SupportChatResponse:
        if state.intent == "account":
            count = await self.session.scalar(
                select(func.count()).select_from(Application).where(Application.owner_user_id == current_user.id)
            )
            return self._response(
                message=f"Your account: {current_user.email or current_user.id}; {int(count or 0)} applications.",
                sources=[SupportChatSource(id="account-own", kind="database", title="Your account")],
            )
        if state.intent == "analytics":
            self._require_permission(current_user, "usage.read_own")
            query = state.analytics
            if query is None:
                return self._clarification()
            try:
                data = await SupportDataService(self.session).query(
                    user_id=current_user.id, application=application, period=state.period, query=query
                )
            except SupportDataQueryError as exc:
                return self._response(status="needs_clarification", message=str(exc))
            source_id = f"usage-summary:{application.id}" if application else "usage-summary"
            response = self._response(
                message=SupportDataService.summary(data),
                sources=[
                    SupportChatSource(id=source_id, kind="database", title=data.title, url="/usage", as_of=data.as_of)
                ],
                actions=[SupportChatAction(label="Open Usage", url="/usage")],
            )
            return response.model_copy(update={"data": data})
        if state.intent == "routing":
            return self._routing_overview(application)
        if state.intent == "billing":
            self._require_permission(current_user, "billing.read")
            return await self._billing_answer(current_user)
        if state.intent == "usage":
            self._require_permission(current_user, "usage.read_own")
            return await self._usage_answer(current_user, application, state.period)
        if state.intent == "application_statistics" and application is not None:
            self._require_permission(current_user, "usage.read_own")
            return await self._application_statistics_answer(current_user, application, state.period)
        if state.intent == "models":
            self._require_permission(current_user, "models.read_available")
            return await self._available_models_answer()
        if state.intent == "application_detail" and application is not None:
            return await self._application_answer(application)
        if state.intent == "application_list":
            return await self._applications_answer(current_user)
        if state.intent == "clarify":
            return self._clarification()
        if state.intent == "faq" and faq_answer is None and is_assistant_model_question(question):
            articles = {article.id: article for article in self.knowledge.articles()}
            source_ids = ["assistant-model"]
            include_capabilities = is_assistant_capability_question(question)
            if include_capabilities:
                source_ids.append("assistant-role")
            selected = [articles[source_id] for source_id in source_ids]
            state.faq_id = selected[0].id
            return self._response(
                message=self.knowledge.assistant_identity_answer(
                    question,
                    include_capabilities=include_capabilities,
                ),
                sources=[SupportChatSource(id=a.id, kind="faq", title=a.title, url=a.url) for a in selected],
            )
        if state.intent == "faq" and faq_answer is not None:
            articles = {article.id: article for article in self.knowledge.articles()}
            if any(source_id not in articles for source_id in faq_answer.source_ids):
                return self._clarification()
            selected = [articles[source_id] for source_id in faq_answer.source_ids]
            state.faq_id = selected[0].id
            sources, actions = self._faq_references(selected, question)
            # For model answers, docs belong in related sources; navigation is
            # explicitly proposed by the model then permission-filtered here.
            actions = [action for action in actions if action.url.startswith("https://")]
            for destination in dict.fromkeys(faq_answer.navigation_ids):
                url, vi_label, en_label, permissions = SUPPORT_NAVIGATION[destination]
                if permissions.intersection(current_user.permissions):
                    actions.append(SupportChatAction(label=vi_label if prefers_vietnamese(question) else en_label, url=url))
            return self._response(
                message=faq_answer.message,
                sources=sources,
                actions=actions,
                suggestions=list(dict.fromkeys(suggestion for article in selected for suggestion in article.suggestions))[:4],
            ).model_copy(update={"grounded_by_agent": True})
        if agent_planned:
            if state.intent == "faq":
                if prefers_vietnamese(question):
                    message = (
                        "Tôi hiểu đây là câu hỏi về Gateway, nhưng lần này chưa xác minh được câu trả lời từ tài liệu. "
                        "Bạn có thể thử lại; tôi sẽ không đoán hoặc chọn một FAQ chỉ vì từ khóa gần giống."
                    )
                else:
                    message = (
                        "I understand that this is a Gateway question, but I could not verify an answer from the "
                        "documentation this time. You can try again; I will not guess or return an FAQ merely because "
                        "a keyword matches."
                    )
                return self._response(
                    status="insufficient_evidence",
                    message=message,
                ).model_copy(update={"choices": [SupportChatChoice(label="Try again", message=question)]})
            return self._clarification()
        article = self.knowledge.retrieve(
            question,
            semantic_scores,
            semantic_threshold=get_settings().support_chat_semantic_threshold,
            reranked_id=reranked_id,
        )
        if article is not None and state.intent == "faq":
            state.faq_id = article.id
            sources, actions = self._faq_references([article], question)
            return self._response(
                message=article.answer,
                sources=sources,
                actions=actions,
                suggestions=list(article.suggestions),
            )

        return self._clarification()

    @staticmethod
    def _faq_references(articles: list[SupportArticle], question: str) -> tuple[list[SupportChatSource], list[SupportChatAction]]:
        vietnamese = prefers_vietnamese(question)
        sources = []
        actions = []
        seen_urls = set()
        for article in articles:
            docs = documentation_links(article.documentation, vietnamese=vietnamese)
            sources.append(SupportChatSource(
                id=article.id, kind=article.kind, title=article.title, url=docs[0][1] if docs else article.url,
            ))
            for title, url in docs:
                if url not in seen_urls:
                    actions.append(SupportChatAction(label=f"{'Đọc tài liệu' if vietnamese else 'Read docs'}: {title}", url=url))
                    seen_urls.add(url)
            if article.url and article.url not in seen_urls:
                label = (f"Đọc tài liệu: {article.title}" if vietnamese else f"Read docs: {article.title}") if article.kind == "documentation" else ("Mở trang liên quan" if vietnamese else "Open related page")
                actions.append(SupportChatAction(label=label, url=article.url))
                seen_urls.add(article.url)
        return sources, actions

    def _clarification(self) -> SupportChatResponse:
        return self._response(
            status="needs_clarification",
            message=(
                "I am not yet sure which area you want to explore. I can explain the Gateway, applications and API keys, "
                "models and routing, or help you review Requests, Usage and Billing within your permissions. "
                "Choose a topic below or describe what you want to check."
            ),
        ).model_copy(
            update={
                "choices": [
                    SupportChatChoice(label="Gateway and routing", message="Explain how the Gateway processes and routes requests"),
                    SupportChatChoice(label="Applications and API keys", message="Show me how to create an application and API key"),
                    SupportChatChoice(label="Requests and charts", message="What request metrics and charts can the assistant provide?"),
                    SupportChatChoice(label="Usage and Billing", message="What is the difference between Usage and Billing?"),
                    SupportChatChoice(label="Model pools and catalog", message="How does a model pool differ from the catalog, and how are models selected?"),
                ]
            }
        )

    @staticmethod
    def _ensure_chat_audience(current_user: CurrentUser) -> None:
        if not current_user.authentication_complete:
            raise SupportChatAccessError("Complete authentication before using support chat")
        if {"admin", "platform_admin"}.intersection(current_user.roles):
            raise SupportChatAccessError("Support chat is not available for this workspace role")

    @staticmethod
    def _require_permission(current_user: CurrentUser, permission: str) -> None:
        if permission not in current_user.permissions:
            raise SupportChatPermissionError("Insufficient permission for support data")

    @staticmethod
    def _matches(question: str, *phrases: str) -> bool:
        return any(phrase in question for phrase in phrases)

    @staticmethod
    def _asks_for_internal_implementation(question: str) -> bool:
        direct_internal_request = any(
            phrase in question
            for phrase in (
                "ma nguon",
                "source code",
                "system prompt",
                "hidden instruction",
                "huong dan an",
                "internal implementation",
            )
        )
        return direct_internal_request

    @staticmethod
    def _asks_for_routing(question: str) -> bool:
        return any(phrase in question for phrase in ("routing", "dinh tuyen", "route"))

    async def _context_application(self, user_id: str, context: SupportChatContext | None) -> Application | None:
        if context is None or context.application_id is None:
            return None
        application = await self.session.scalar(
            select(Application).where(
                Application.id == context.application_id,
                Application.owner_user_id == user_id,
            )
        )
        if application is None:
            raise SupportChatNotFoundError("Application not found")
        return application

    async def _named_application(self, user_id: str, question: str) -> Application | None:
        if self._matches(question, "application nay", "ung dung nay", "app nay"):
            return None
        rows = list(await self.session.scalars(select(Application).where(Application.owner_user_id == user_id)))
        matches = []
        for row in rows:
            score = max(
                (
                    len(re.sub(r"[\s_-]+", "", name))
                    for name in (normalize_support_text(row.name), normalize_support_text(row.slug))
                    if name and self._contains_name(question, name)
                ),
                default=0,
            )
            if score:
                matches.append((score, row))
        if not matches:
            return None
        highest = max(score for score, _ in matches)
        winners = [row for score, row in matches if score == highest]
        return winners[0] if len(winners) == 1 else None

    @staticmethod
    def _contains_name(question: str, name: str) -> bool:
        # Treat harmless spacing/hyphen differences as equivalent: "test23",
        # "test 23" and "test-23" can all select the same owned application.
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", question) is not None:
            return True
        if not re.fullmatch(r"[a-z0-9\s_-]+", name):
            return False
        parts = re.findall(r"[a-z]+|\d+", name)
        if not parts:
            return False
        pattern = r"[\s_-]*".join(re.escape(part) for part in parts)
        return re.search(r"(?<!\w)" + pattern + r"(?!\w)", question) is not None

    @staticmethod
    def _has_explicit_application_name(question: str) -> bool:
        match = re.search(r"\b(?:application|ung dung|app)\s+([\w-]+)", question)
        return bool(match and match.group(1) not in {"nay", "do", "cua", "hien", "nao", "da", "dang", "moi"})

    async def _single_application(self, user_id: str) -> Application | None:
        rows = list(
            await self.session.scalars(select(Application).where(Application.owner_user_id == user_id).limit(2))
        )
        return rows[0] if len(rows) == 1 else None

    def _routing_overview(self, application: Application | None) -> SupportChatResponse:
        context_note = (
            f" Với application {application.name}, bạn có thể xem cấu hình tại trang Settings."
            if application is not None
            else ""
        )
        return self._response(
            message=(
                "Ở mức tổng quan, Gateway đọc ràng buộc và cấu hình của application, chọn một model phù hợp "
                "trong các model được phép, có thể dùng fallback khi cần, rồi ghi lại kết quả, token, chi phí và độ trễ. "
                "Tôi không cung cấp chi tiết logic triển khai hoặc quy tắc nội bộ của routing." + context_note
            ),
            sources=[
                SupportChatSource(id="routing-overview", kind="faq", title="Tổng quan routing", url="/applications")
            ],
            actions=[SupportChatAction(label="Mở Applications", url="/applications")],
        )

    async def _billing_answer(self, current_user: CurrentUser) -> SupportChatResponse:
        account = await self.session.scalar(
            select(BillingAccountRecord).where(BillingAccountRecord.user_id == current_user.id)
        )
        if account is None:
            return self._response(
                message="Tài khoản của bạn chưa có dữ liệu billing. Chatbot không tự tạo billing account khi chỉ đọc thông tin.",
                sources=[
                    SupportChatSource(id="billing-account", kind="database", title="Billing account", url="/billing")
                ],
                actions=[SupportChatAction(label="Mở Billing", url="/billing")],
            )
        return self._response(
            message=(
                f"Số dư hiện tại của bạn là {Decimal(account.balance):.6f} {account.currency}. "
                f"Dữ liệu được đọc lúc {self._timestamp(account.updated_at)}."
            ),
            sources=[
                SupportChatSource(
                    id=f"billing-account:{account.id}",
                    kind="database",
                    title="Billing account của bạn",
                    url="/billing",
                    as_of=self._timestamp(account.updated_at),
                )
            ],
            actions=[SupportChatAction(label="Mở Billing", url="/billing")],
        )

    async def _usage_answer(
        self, current_user: CurrentUser, application: Application | None, question: str
    ) -> SupportChatResponse:
        start, end, label = self._usage_period(question)
        conditions = [
            Application.owner_user_id == current_user.id,
            UsageEventRecord.created_at >= start,
            UsageEventRecord.created_at < end,
        ]
        if application is not None:
            conditions.append(UsageEventRecord.application_id == application.id)
        result = await self.session.execute(
            select(
                func.count(func.distinct(UsageEventRecord.request_id)),
                func.coalesce(func.sum(UsageEventRecord.input_tokens), 0),
                func.coalesce(func.sum(UsageEventRecord.output_tokens), 0),
                func.coalesce(func.sum(UsageEventRecord.cost_usd), 0.0),
            )
            .join(Application, Application.id == UsageEventRecord.application_id)
            .where(*conditions)
        )
        requests, input_tokens, output_tokens, cost_usd = result.one()
        target = f"application {application.name}" if application is not None else "các application của bạn"
        url = "/usage" if application is None else f"/usage?application_id={application.id}"
        now = datetime.now(UTC)
        return self._response(
            message=(
                f"{label} của {target}: {int(requests or 0)} request, {int(input_tokens or 0):,} input tokens, "
                f"{int(output_tokens or 0):,} output tokens và {float(cost_usd or 0):.6f} USD. "
                f"Khoảng thời gian theo múi giờ workspace (UTC offset {get_settings().support_chat_utc_offset_minutes} phút), đọc lúc {self._timestamp(now)}."
            ),
            sources=[
                SupportChatSource(
                    id="usage-summary" if application is None else f"usage-summary:{application.id}",
                    kind="database",
                    title=f"Usage {label.lower()}",
                    url=url,
                    as_of=self._timestamp(now),
                )
            ],
            actions=[SupportChatAction(label="Mở Usage", url=url)],
        )

    async def _application_statistics_answer(
        self, current_user: CurrentUser, application: Application, question: str
    ) -> SupportChatResponse:
        start, end, label = self._usage_period(question)
        usage_row = (
            await self.session.execute(
                select(
                    func.coalesce(func.sum(UsageEventRecord.input_tokens), 0),
                    func.coalesce(func.sum(UsageEventRecord.output_tokens), 0),
                    func.coalesce(func.sum(UsageEventRecord.cost_usd), 0.0),
                )
                .join(Application, Application.id == UsageEventRecord.application_id)
                .where(
                    Application.owner_user_id == current_user.id,
                    Application.id == application.id,
                    UsageEventRecord.created_at >= start,
                    UsageEventRecord.created_at < end,
                )
            )
        ).one()
        request_row = (
            await self.session.execute(
                select(
                    func.count(GatewayRequestRecord.request_id),
                    func.coalesce(
                        func.sum(case((GatewayRequestRecord.status == "completed", 1), else_=0)),
                        0,
                    ),
                    func.coalesce(func.avg(GatewayRequestRecord.total_latency_ms), 0.0),
                )
                .join(Application, Application.id == GatewayRequestRecord.application_id)
                .where(
                    Application.owner_user_id == current_user.id,
                    Application.id == application.id,
                    GatewayRequestRecord.started_at >= start,
                    GatewayRequestRecord.started_at < end,
                )
            )
        ).one()
        requests, completed, latency_ms = request_row
        success_rate = 100 * int(completed or 0) / int(requests) if requests else 0
        now = datetime.now(UTC)
        url = f"/usage?application_id={application.id}"
        return self._response(
            message=(
                f"{label} của application {application.name}: {int(requests or 0)} request, "
                f"{success_rate:.1f}% hoàn thành, độ trễ trung bình {float(latency_ms or 0):.1f} ms; "
                f"{int(usage_row[0] or 0):,} input tokens, {int(usage_row[1] or 0):,} output tokens, "
                f"chi phí {float(usage_row[2] or 0):.6f} USD. "
                f"Khoảng thời gian theo múi giờ workspace (UTC offset {get_settings().support_chat_utc_offset_minutes} phút), đọc lúc {self._timestamp(now)}."
            ),
            sources=[
                SupportChatSource(
                    id=f"usage-summary:{application.id}",
                    kind="database",
                    title=f"Thống kê application {application.name}",
                    url=url,
                    as_of=self._timestamp(now),
                )
            ],
            actions=[
                SupportChatAction(label="Mở Usage", url=url),
                SupportChatAction(label="Mở application", url=f"/applications/{application.id}"),
            ],
        )

    async def _available_models_answer(self) -> SupportChatResponse:
        rows = list(
            (
                await self.session.execute(
                    select(Model, Provider)
                    .join(Provider, Provider.id == Model.provider_id)
                    .where(Model.status == "active", Provider.status == "active", visible_to(None))
                    .order_by(Provider.display_name, Model.display_name)
                    .limit(5)
                )
            ).all()
        )
        if not rows:
            message = "Hiện chưa có model active nào trong catalog mà chatbot có thể hiển thị."
        else:
            displayed = "; ".join(
                f"{model.display_name} ({provider.display_name}, {', '.join(model.capabilities_json or []) or 'không có capability khai báo'})"
                for model, provider in rows
            )
            message = (
                f"Một số model active hiện có: {displayed}. Đây là trạng thái catalog; "
                "hãy xem cấu hình application để biết model nào được phép dùng cho application cụ thể."
            )
        now = datetime.now(UTC)
        return self._response(
            message=message,
            sources=[
                SupportChatSource(
                    id="available-models",
                    kind="database",
                    title="Available models",
                    url="/available-models",
                    as_of=self._timestamp(now),
                )
            ],
            actions=[SupportChatAction(label="Mở Available models", url="/available-models")],
        )

    async def _application_answer(self, application: Application) -> SupportChatResponse:
        config = await self.session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application.id)
        )
        url = f"/applications/{application.id}"
        if config is None:
            config_text = "Application chưa có gateway configuration được ghi nhận."
        else:
            budget = "chưa đặt" if config.budget_ceiling is None else f"{config.budget_ceiling:.6f} USD"
            config_text = (
                f"Mục tiêu routing: {config.routing_objective}; fallback: "
                f"{'bật' if config.fallback_enabled else 'tắt'}; budget ceiling: {budget}; "
                f"giới hạn output: {config.max_output_tokens} tokens."
            )
        return self._response(
            message=(
                f"Application {application.name}: trạng thái {application.status}, môi trường {application.environment}. "
                + config_text
            ),
            sources=[
                SupportChatSource(
                    id=f"application:{application.id}",
                    kind="database",
                    title=f"Application {application.name}",
                    url=url,
                    as_of=self._timestamp(application.updated_at),
                )
            ],
            actions=[SupportChatAction(label="Mở application", url=url)],
        )

    async def _applications_answer(self, current_user: CurrentUser, *, introduction: str = "") -> SupportChatResponse:
        total = int(
            await self.session.scalar(
                select(func.count()).select_from(Application).where(Application.owner_user_id == current_user.id)
            )
            or 0
        )
        rows = list(
            await self.session.scalars(
                select(Application)
                .where(Application.owner_user_id == current_user.id)
                .order_by(Application.updated_at.desc())
                .limit(10)
            )
        )
        if not rows:
            message = "Bạn chưa có application nào. Bạn có thể tạo application mới từ trang Applications."
        else:
            message = (
                introduction
                + f"Bạn có {total} application. Gần đây: "
                + "; ".join(
                    f"{application.name} ({application.status}, {application.environment})" for application in rows
                )
                + ("; ..." if total > len(rows) else ".")
            )
        response = self._response(
            message=message,
            sources=[
                SupportChatSource(
                    id="applications-own", kind="database", title="Applications của bạn", url="/applications"
                )
            ],
            actions=[
                SupportChatAction(label="Mở Applications", url="/applications"),
                *[
                    SupportChatAction(label=f"Mở {application.name}", url=f"/applications/{application.id}")
                    for application in rows[:3]
                ],
            ],
        )
        if introduction:
            response.choices = [
                SupportChatChoice(label=application.name[:120], message=f"Tôi chọn application {application.name}")
                for application in rows[:6]
            ]
        return response

    @staticmethod
    def suggestions(current_user: CurrentUser, context: SupportChatContext | None) -> list[str]:
        if context and context.application_id:
            prompts = ["Cấu hình application này"]
            if "usage.read_own" in current_user.permissions:
                prompts.insert(0, "Biến động chi phí theo từng request của application này trong 7 ngày")
                prompts.insert(1, "Vẽ biểu đồ latency theo từng request của application này trong 7 ngày")
            if "billing.read" in current_user.permissions:
                prompts.append("Số dư credit của tôi là bao nhiêu?")
            prompts.append("Hướng dẫn tạo API key")
            return prompts[:3]

        prompts = ["Liệt kê application của tôi"]
        if "usage.read_own" in current_user.permissions:
            if context and context.page == "usage":
                prompts.append("Biến động chi phí theo từng request trong 7 ngày")
                prompts.append("Vẽ biểu đồ latency theo từng request trong 7 ngày")
            else:
                prompts.append("Vẽ biểu đồ chi phí trong 7 ngày")
        if "billing.read" in current_user.permissions:
            prompts.append("Số dư credit của tôi là bao nhiêu?")
        prompts.append("Hướng dẫn tạo API key")
        return prompts[:3]

    @staticmethod
    def _usage_period(question: str) -> tuple[datetime, datetime, str]:
        now = datetime.now(UTC)
        local = now.astimezone(timezone(timedelta(minutes=get_settings().support_chat_utc_offset_minutes)))
        start_of_day = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        period = (
            question
            if question in {"today", "yesterday", "this_week", "this_month", "last_7_days", "last_30_days"}
            else explicit_support_period(question)
        )
        if period == "today":
            return start_of_day, now, "Hôm nay"
        if period == "yesterday":
            return start_of_day - timedelta(days=1), start_of_day, "Hôm qua"
        if period == "this_week":
            return start_of_day - timedelta(days=local.weekday()), now, "Tuần này"
        if period == "this_month":
            return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC), now, "Tháng này"
        if period == "last_30_days":
            return now - timedelta(days=30), now, "30 ngày gần nhất"
        return now - timedelta(days=7), now, "7 ngày gần nhất"

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _response(
        *,
        message: str,
        status: str = "answered",
        sources: list[SupportChatSource] | None = None,
        actions: list[SupportChatAction] | None = None,
        suggestions: list[str] | None = None,
    ) -> SupportChatResponse:
        return SupportChatResponse(
            request_id=str(uuid4()),
            status=status,
            message=message,
            sources=sources or [],
            actions=actions or [],
            suggestions=suggestions or [],
        )
