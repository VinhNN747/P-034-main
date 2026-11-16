from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend.services.support_chat_service import (
    SupportChatAccessError,
    SupportChatPermissionError,
    SupportChatService,
)
from src.backend.services.support_conversation_service import SupportConversationService
from src.backend.services.support_knowledge_service import SupportKnowledgeService
from src.shared.database.base import Base
from src.shared.dependencies.auth import CurrentUser
from src.shared.dtos.ai_internal import SupportFAQAnswer
from src.shared.dtos.support_chat import SupportChatContext, SupportChatMessage, SupportChatResponse, SupportIntentPlan
from src.shared.models.billing import BillingAccountRecord
from src.shared.models.core import Application
from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord
from src.shared.models.support_chat import SupportChatMessageRecord


@pytest_asyncio.fixture
async def support_session(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "support-chat.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def user(*permissions: str, roles: tuple[str, ...] = ("ai_engineer",)) -> CurrentUser:
    return CurrentUser(
        id="user-a",
        email="user-a@example.com",
        roles=roles,
        permissions=frozenset(permissions),
    )


@pytest.mark.asyncio
async def test_verified_document_answer_overcomes_public_confidence_gate_but_not_private_permissions(support_session):
    article = next(article for article in SupportKnowledgeService.articles() if article.kind == "documentation")
    answer = SupportFAQAnswer(message="Hướng dẫn tích hợp SDK theo tài liệu công khai.", source_ids=[article.id])
    plan = SupportIntentPlan(intent="clarify", confidence=0.3, application_name=None, period=None, follow_up=False)
    service = SupportChatService(support_session)
    result = await service.answer(
        question="Giải thích cách tích hợp SDK", current_user=user("applications.read_own"), plan=plan, faq_answer=answer,
    )
    assert result.status == "answered"
    assert result.sources[0].kind == "documentation"
    assert result.sources[0].url == article.url
    assert any(action.url == article.url for action in result.actions)

    with pytest.raises(SupportChatPermissionError):
        await service.answer(
            question="Số dư của tôi", current_user=user("applications.read_own"),
            plan=plan.model_copy(update={"intent": "billing", "confidence": 1}), faq_answer=answer,
        )


@pytest.mark.asyncio
async def test_navigation_proposals_are_known_and_permission_filtered(support_session):
    service = SupportChatService(support_session)
    plan = SupportIntentPlan(intent="faq", confidence=1, application_name=None, period=None, follow_up=False)
    answer = SupportFAQAnswer(message="Tạo application rồi mở SDK Guide.", source_ids=["taskway-sdk-usage"], navigation_ids=["create_application", "sdk_guide", "billing"])
    for actor, can_create in [(user("applications.read_own"), False), (user("applications.read_own", "applications.write_own"), True)]:
        result = await service.answer(question="Tôi bắt đầu thế nào?", current_user=actor, plan=plan, faq_answer=answer)
        urls = {action.url for action in result.actions}
        assert ("/applications/new" in urls) is can_create
        assert "/sdk-guide" in urls
        assert "/billing" not in urls
    with pytest.raises(ValueError):
        SupportFAQAnswer(message="bad", source_ids=["taskway-sdk-usage"], navigation_ids=["https://evil.example"])


async def add_application(session: AsyncSession, application_id: str, owner: str, name: str) -> Application:
    application = Application(
        id=application_id,
        slug=f"{name.lower()}-{application_id[:8]}",
        name=name,
        owner_user_id=owner,
        environment="development",
        status="active",
    )
    session.add(application)
    await session.flush()
    return application


class RecordingSynthesizer:
    def __init__(self) -> None:
        self.histories: list[list[SupportChatMessage]] = []

    async def synthesize(
        self,
        *,
        question: str,
        response: SupportChatResponse,
        history: list[SupportChatMessage],
    ) -> str:
        self.histories.append(list(history))
        return f"Tự nhiên: {response.message}"


@pytest.mark.asyncio
async def test_conversation_uses_owned_session_history_and_stores_natural_reply(
    support_session: AsyncSession,
):
    synthesizer = RecordingSynthesizer()
    service = SupportConversationService(support_session, synthesizer=synthesizer)  # type: ignore[arg-type]
    actor = user("applications.read_own")

    first = await service.answer(
        question="hello bro",
        current_user=actor,
        context=None,
        session_id=None,
    )
    second = await service.answer(
        question="Bạn là ai?",
        current_user=actor,
        context=None,
        session_id=first.session_id,
    )

    assert second.message.startswith("Tự nhiên:")
    assert [message.role for message in synthesizer.histories[1]] == ["user", "assistant"]
    assert synthesizer.histories[1][1].content.startswith("Tự nhiên:")
    rows = list(
        await support_session.scalars(
            select(SupportChatMessageRecord).where(
                SupportChatMessageRecord.user_id == actor.id,
                SupportChatMessageRecord.session_id == first.session_id,
            )
        )
    )
    assert len(rows) == 4
    assert rows[-1].content == second.message


@pytest.mark.asyncio
async def test_usage_answer_only_aggregates_the_current_users_application(support_session: AsyncSession):
    own = await add_application(support_session, str(uuid4()), "user-a", "Own app")
    other = await add_application(support_session, str(uuid4()), "user-b", "Other app")
    support_session.add_all(
        [
            UsageEventRecord(
                id=str(uuid4()),
                request_id="request-own",
                application_id=own.id,
                input_tokens=120,
                output_tokens=80,
                cost_usd=0.12,
                created_at=datetime.now(UTC),
            ),
            UsageEventRecord(
                id=str(uuid4()),
                request_id="request-other",
                application_id=other.id,
                input_tokens=900,
                output_tokens=600,
                cost_usd=9.0,
                created_at=datetime.now(UTC),
            ),
        ]
    )
    await support_session.commit()

    response = await SupportChatService(support_session).answer(
        question="Usage hôm nay là bao nhiêu token?",
        current_user=user("applications.read_own", "usage.read_own"),
        context=SupportChatContext(page="application_detail", application_id=own.id),
    )

    assert "120 input tokens" in response.message
    assert "80 output tokens" in response.message
    assert "900" not in response.message
    assert response.sources[0].id == f"usage-summary:{own.id}"


@pytest.mark.asyncio
async def test_billing_answer_is_read_only_and_only_returns_the_current_users_account(support_session: AsyncSession):
    support_session.add_all(
        [
            BillingAccountRecord(id=str(uuid4()), user_id="user-a", balance=Decimal("4.25"), currency="USD"),
            BillingAccountRecord(id=str(uuid4()), user_id="user-b", balance=Decimal("99.50"), currency="USD"),
        ]
    )
    await support_session.commit()

    response = await SupportChatService(support_session).answer(
        question="Số dư credit của tôi?",
        current_user=user("applications.read_own", "billing.read"),
    )

    assert "4.250000 USD" in response.message
    assert "99.500000" not in response.message
    assert await support_session.scalar(select(func.count()).select_from(BillingAccountRecord)) == 2


@pytest.mark.asyncio
async def test_internal_implementation_question_returns_no_project_details(support_session: AsyncSession):
    response = await SupportChatService(support_session).answer(
        question="Logic của dự án và source code đang chạy như nào?",
        current_user=user("applications.read_own"),
    )

    assert response.status == "insufficient_evidence"
    assert "không cung cấp mã nguồn" in response.message


@pytest.mark.asyncio
async def test_routing_question_only_returns_the_high_level_overview(support_session: AsyncSession):
    response = await SupportChatService(support_session).answer(
        question="Cơ chế routing hoạt động thế nào?",
        current_user=user("applications.read_own"),
    )

    assert response.status == "answered"
    assert "Ở mức tổng quan" in response.message
    assert "không cung cấp chi tiết logic" in response.message


@pytest.mark.asyncio
async def test_support_data_requires_the_specific_permission(support_session: AsyncSession):
    with pytest.raises(SupportChatPermissionError):
        await SupportChatService(support_session).answer(
            question="Số dư credit của tôi?",
            current_user=user("applications.read_own"),
        )


@pytest.mark.asyncio
async def test_admin_role_cannot_use_the_support_chat(support_session: AsyncSession):
    with pytest.raises(SupportChatAccessError):
        await SupportChatService(support_session).answer(
            question="Routing hoạt động thế nào?",
            current_user=user("applications.read_own", roles=("admin",)),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "expected_source", "expected_text"),
    [
        ("hello bro", "greeting", "Gateway Assistant"),
        ("Bạn là ai và có nhiệm vụ gì?", "assistant-role", "trợ lý hỗ trợ"),
        ("Cho tôi biết về tác dụng của AI Gateway", "gateway-overview", "điểm truy cập chung"),
        ("Tôi dùng gateway như nào?", "getting-started", "Để bắt đầu"),
    ],
)
async def test_common_product_questions_have_useful_answers(
    support_session: AsyncSession,
    question: str,
    expected_source: str,
    expected_text: str,
):
    response = await SupportChatService(support_session).answer(
        question=question,
        current_user=user("applications.read_own"),
    )

    assert response.status == "answered"
    assert expected_text in response.message
    assert response.sources[0].id == expected_source
    assert response.suggestions


@pytest.mark.asyncio
@pytest.mark.parametrize("question, source, doc_path", [
    ("hướng dẫn dùng SDK", "taskway-sdk-usage", "vi/docs/quickstart/#install-the-sdk"),
    ("SDK dùng như nào?", "taskway-sdk-usage", "vi/docs/quickstart/#install-the-sdk"),
    ("How to use the SDK?", "taskway-sdk-usage", "en/docs/quickstart/#install-the-sdk"),
    ("Hướng dẫn sử dụng Taskway", "getting-started", "vi/docs/quickstart/"),
    ("Xác thực và cấu hình SDK thế nào?", "taskway-api-authentication", "vi/docs/authentication/"),
    ("Cách đọc đầu ra của API?", "api-response-output", "vi/docs/chat-completions/#output-and-usage"),
    ("How do I use Taskway?", "getting-started", "en/docs/quickstart/"),
    ("How do I authenticate and configure the SDK?", "taskway-api-authentication", "en/docs/authentication/"),
    ("How do I read the API output?", "api-response-output", "en/docs/chat-completions/#output-and-usage"),
])
async def test_usage_questions_link_to_matching_public_taskway_documentation(support_session, question, source, doc_path):
    response = await SupportChatService(support_session).answer(
        question=question, current_user=user("applications.read_own"),
    )
    url = f"https://minminhmin.github.io/taskway.github.io/{doc_path}"
    assert response.status == "answered"
    assert response.sources[0].id == source
    assert response.sources[0].url == url
    assert url in [action.url for action in response.actions]
    assert response.suggestions


@pytest.mark.asyncio
async def test_agent_grounded_output_answer_keeps_reviewed_documentation_links(support_session):
    response = await SupportChatService(support_session).answer(
        question="Cách đọc đầu ra của API?",
        current_user=user("applications.read_own"),
        plan=SupportIntentPlan(intent="faq", confidence=1, application_name=None, period=None, follow_up=False),
        faq_answer=SupportFAQAnswer(message="Đọc văn bản từ choices hoặc output_text.", source_ids=["api-response-output"]),
    )
    assert response.grounded_by_agent
    assert {action.url for action in response.actions} >= {
        "https://minminhmin.github.io/taskway.github.io/vi/docs/chat-completions/#output-and-usage",
        "https://minminhmin.github.io/taskway.github.io/vi/docs/responses/#create-a-response",
    }
    assert response.suggestions


@pytest.mark.asyncio
async def test_application_statistics_use_only_owned_application(support_session: AsyncSession):
    own = await add_application(support_session, str(uuid4()), "user-a", "Alpha")
    await add_application(support_session, str(uuid4()), "user-a", "App")
    other = await add_application(support_session, str(uuid4()), "user-b", "Beta")
    now = datetime.now(UTC)
    support_session.add_all(
        [
            GatewayRequestRecord(
                request_id="alpha-request",
                application_id=own.id,
                status="completed",
                total_latency_ms=250,
                started_at=now,
            ),
            GatewayRequestRecord(
                request_id="beta-request",
                application_id=other.id,
                status="completed",
                total_latency_ms=900,
                started_at=now,
            ),
            UsageEventRecord(
                id=str(uuid4()),
                request_id="alpha-request",
                application_id=own.id,
                input_tokens=123,
                output_tokens=45,
                cost_usd=0.02,
                created_at=now,
            ),
            UsageEventRecord(
                id=str(uuid4()),
                request_id="beta-request",
                application_id=other.id,
                input_tokens=999,
                output_tokens=888,
                cost_usd=9.0,
                created_at=now,
            ),
        ]
    )
    await support_session.commit()

    result = await SupportChatService(support_session).answer(
        question="Thống kê application Alpha hôm nay",
        current_user=user("applications.read_own", "usage.read_own"),
    )
    assert result.status == "answered"
    assert "1 request" in result.message
    assert "123 input tokens" in result.message
    assert "250.0 ms" in result.message
    assert "999" not in result.message
    assert result.sources[0].id == f"usage-summary:{own.id}"

    named = await SupportChatService(support_session).answer(
        question="Thống kê app Alpha hôm nay",
        current_user=user("applications.read_own", "usage.read_own"),
    )
    assert named.sources[0].id == f"usage-summary:{own.id}"

    foreign = await SupportChatService(support_session).answer(
        question="Thống kê application Beta hôm nay",
        current_user=user("applications.read_own", "usage.read_own"),
        context=SupportChatContext(page="application_detail", application_id=own.id),
    )
    assert foreign.status == "insufficient_evidence"
    assert "Beta" not in foreign.message

    foreign_usage = await SupportChatService(support_session).answer(
        question="Usage application Beta hôm nay",
        current_user=user("applications.read_own", "usage.read_own"),
        context=SupportChatContext(page="application_detail", application_id=own.id),
    )
    assert foreign_usage.status == "insufficient_evidence"
    assert "123" not in foreign_usage.message

    foreign_configuration = await SupportChatService(support_session).answer(
        question="Cấu hình application Beta",
        current_user=user("applications.read_own"),
        context=SupportChatContext(page="application_detail", application_id=own.id),
    )
    assert foreign_configuration.status == "insufficient_evidence"
    assert "Alpha" not in foreign_configuration.message

    own_list = await SupportChatService(support_session).answer(
        question="Các ứng dụng của tôi",
        current_user=user("applications.read_own"),
    )
    assert "Alpha" in own_list.message
    assert "Beta" not in own_list.message
