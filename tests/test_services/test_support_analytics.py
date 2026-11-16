import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.backend.services.support_chat_service import (
    SupportChatNotFoundError,
    SupportChatPermissionError,
    SupportChatService,
)
from src.backend.services.support_conversation_service import SupportConversationService
from src.backend.services.support_data_service import SupportDataQueryError, SupportDataService
from src.backend.services.support_intent_service import local_analytics_query
from src.backend.services.support_knowledge_service import SupportKnowledgeService
from src.backend_ai.services.support_understanding_service import SupportUnderstandingService
from src.shared.config import Settings
from src.shared.database.base import Base
from src.shared.dependencies.auth import CurrentUser
from src.shared.dtos.ai_internal import SupportPlannerOutput, SupportUnderstandingInput, SupportUnderstandingOutput
from src.shared.dtos.support_chat import (
    SupportAnalyticsQuery,
    SupportChatContext,
    SupportConversationState,
    SupportIntentPlan,
)
from src.shared.models.core import Application
from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord
from src.shared.services.support_safety import safe_support_history


def actor(owner="user-a", usage=True):
    return CurrentUser(
        id=owner,
        email=f"{owner}@example.com",
        roles=("user",),
        permissions=frozenset({"applications.read_own", *({"usage.read_own"} if usage else set())}),
    )


def query(operation="timeseries", metric="cost_usd", group_by="day", chart_type=None, **kwargs):
    return SupportAnalyticsQuery(
        operation=operation,
        metric=metric,
        group_by=group_by,
        chart_type=chart_type,
        start_date=kwargs.get("start_date"),
        end_date=kwargs.get("end_date"),
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analytics.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def seed(db):
    own = Application(id=str(uuid4()), slug="alpha", name="Alpha", owner_user_id="user-a")
    foreign = Application(id=str(uuid4()), slug="beta", name="Beta", owner_user_id="user-b")
    db.add_all([own, foreign])
    await db.flush()
    now = datetime.now(UTC) - timedelta(minutes=1)
    db.add_all(
        [
            GatewayRequestRecord(
                request_id="own-1", application_id=own.id, status="completed", total_latency_ms=100, started_at=now
            ),
            GatewayRequestRecord(
                request_id="own-2", application_id=own.id, status="failed", total_latency_ms=300, started_at=now
            ),
            GatewayRequestRecord(
                request_id="foreign-1",
                application_id=foreign.id,
                status="completed",
                total_latency_ms=999,
                started_at=now,
            ),
        ]
    )
    await db.flush()
    db.add_all(
        [
            UsageEventRecord(
                request_id="own-1",
                application_id=own.id,
                model="m1",
                input_tokens=10,
                output_tokens=2,
                cost_usd=0.1,
                created_at=now,
            ),
            UsageEventRecord(
                request_id="own-1",
                application_id=own.id,
                model="m2",
                input_tokens=20,
                output_tokens=3,
                cost_usd=0.2,
                created_at=now,
            ),
            UsageEventRecord(
                request_id="own-2",
                application_id=own.id,
                model="m2",
                input_tokens=30,
                output_tokens=4,
                cost_usd=0.3,
                created_at=now,
            ),
            UsageEventRecord(
                request_id="foreign-1",
                application_id=foreign.id,
                model="foreign",
                input_tokens=999,
                output_tokens=999,
                cost_usd=99,
                created_at=now,
            ),
        ]
    )
    await db.commit()
    return own, foreign


@pytest.mark.asyncio
async def test_metrics_use_correct_grain_without_join_multiplication(db):
    own, _ = await seed(db)
    service = SupportDataService(db)
    for metric, total in [
        ("cost_usd", 0.6),
        ("requests", 2),
        ("tokens", 69),
        ("success_rate", 50),
        ("latency_ms", 200),
    ]:
        result = await service.query(
            user_id="user-a", application=own, period="last_7_days", query=query(metric=metric)
        )
        assert result.total == pytest.approx(total)
        nonzero = [row.value for row in result.rows if row.requests]
        assert nonzero == [pytest.approx(total)]
        assert "application_ids" not in result.model_dump()
    models = await service.query(
        user_id="user-a", application=None, period="last_7_days", query=query("breakdown", group_by="model")
    )
    assert {row.label: round(row.value, 3) for row in models.rows} == {"m1": 0.1, "m2": 0.5}
    with pytest.raises(SupportDataQueryError, match="một request"):
        await service.query(
            user_id="user-a", application=None, period="last_7_days", query=query("breakdown", "requests", "model")
        )


@pytest.mark.asyncio
async def test_cost_and_latency_can_be_plotted_per_request_without_cross_account_data(db):
    own, _ = await seed(db)
    service = SupportDataService(db)

    costs = await service.query(
        user_id="user-a",
        application=own,
        period="last_7_days",
        query=query(metric="cost_usd", group_by="request", chart_type="line"),
    )
    assert costs.dimension == "request"
    assert [(row.label, row.value) for row in costs.rows] == [
        ("own-1", pytest.approx(0.3)),
        ("own-2", pytest.approx(0.3)),
    ]
    assert "2 usage events" in costs.rows[0].detail
    assert all("foreign" not in row.label for row in costs.rows)

    latency = await service.query(
        user_id="user-a",
        application=own,
        period="last_7_days",
        query=query(metric="latency_ms", group_by="request", chart_type="line"),
    )
    assert [(row.label, row.value) for row in latency.rows] == [("own-1", 100), ("own-2", 300)]
    assert latency.rows[0].detail.endswith("completed")
    assert latency.rows[1].detail.endswith("failed")


def test_request_series_intent_understands_price_and_latency_variation():
    state = SupportConversationState()
    price = local_analytics_query("Biến động giá theo từng request trong 7 ngày", state)
    assert price is not None
    assert (price.operation, price.metric, price.group_by, price.chart_type) == (
        "timeseries", "cost_usd", "request", "line"
    )
    latency = local_analytics_query("So sánh latency per request", state)
    assert latency is not None
    assert (latency.operation, latency.metric, latency.group_by, latency.chart_type) == (
        "timeseries", "latency_ms", "request", None
    )


@pytest.mark.asyncio
async def test_support_chat_returns_a_request_level_chart_from_natural_language(db):
    own, _ = await seed(db)
    response = await SupportChatService(db).answer(
        question="Biến động giá theo từng request trong 7 ngày",
        current_user=actor(),
        context=SupportChatContext(page="application_detail", application_id=own.id),
    )
    assert response.status == "answered"
    assert response.data is not None
    assert (response.data.metric, response.data.dimension, response.data.chart_type) == (
        "cost_usd", "request", "line"
    )
    assert [row.label for row in response.data.rows] == ["own-1", "own-2"]


@pytest.mark.asyncio
async def test_request_series_rejects_metrics_that_have_no_per_request_variation(db):
    own, _ = await seed(db)
    with pytest.raises(SupportDataQueryError, match="không có ý nghĩa"):
        await SupportDataService(db).query(
            user_id="user-a",
            application=own,
            period="last_7_days",
            query=query(metric="success_rate", group_by="request", chart_type="line"),
        )


@pytest.mark.asyncio
async def test_range_is_exclusive_and_buckets_use_workspace_timezone(db):
    own, _ = await seed(db)
    # UTC+7 midnight at 17:00 UTC; a timestamp immediately before goes to the previous day.
    db.add_all(
        [
            UsageEventRecord(
                request_id="own-1",
                application_id=own.id,
                cost_usd=1,
                created_at=datetime(2026, 1, 1, 16, 59, tzinfo=UTC),
            ),
            UsageEventRecord(
                request_id="own-1",
                application_id=own.id,
                cost_usd=2,
                created_at=datetime(2026, 1, 1, 17, 0, tzinfo=UTC),
            ),
            UsageEventRecord(
                request_id="own-1",
                application_id=own.id,
                cost_usd=10,
                created_at=datetime(2026, 1, 2, 17, 0, tzinfo=UTC),
            ),
        ]
    )
    await db.commit()
    result = await SupportDataService(db).query(
        user_id="user-a", application=own, period="today", query=query(start_date="2026-01-01", end_date="2026-01-03")
    )
    assert result.total == 3
    assert [(row.label, row.value) for row in result.rows] == [("2026-01-01", 1), ("2026-01-02", 2)]


@pytest.mark.asyncio
async def test_empty_rates_and_zero_baseline_are_not_fabricated(db):
    own, _ = await seed(db)
    empty = await SupportDataService(db).query(
        user_id="nobody", application=None, period="today", query=query(metric="success_rate")
    )
    assert empty.total is None and empty.rows == []
    db.add(
        UsageEventRecord(
            request_id="own-1", application_id=own.id, cost_usd=0, created_at=datetime.now(UTC) - timedelta(days=8)
        )
    )
    await db.commit()
    compared = await SupportDataService(db).query(
        user_id="user-a", application=own, period="last_7_days", query=query("compare")
    )
    assert compared.baseline == 0 and compared.delta == pytest.approx(0.6)
    assert compared.percent_change is None
    json.dumps(compared.model_dump(mode="json"), allow_nan=False)


@pytest.mark.asyncio
async def test_explicit_chart_and_followup_return_data_without_synthesis(db):
    own, _ = await seed(db)
    understanding = AsyncMock()
    understanding.understand.return_value = SupportUnderstandingOutput()
    synthesis = AsyncMock()
    chat = SupportConversationService(db, understanding=understanding, synthesizer=synthesis)
    first = await chat.answer(
        question="Vẽ biểu đồ chi phí application Alpha theo ngày trong 7 ngày",
        current_user=actor(),
        context=None,
        session_id=None,
    )
    assert first.data.total == pytest.approx(0.6)
    assert first.data.chart_type == "line"
    synthesis.synthesize.assert_not_awaited()
    second = await chat.answer(question="Còn hôm qua?", current_user=actor(), context=None, session_id=first.session_id)
    assert second.data.metric == "cost_usd"
    assert second.conversation_state.period == "yesterday"
    detail = await chat.get_session(actor(), first.session_id)
    assert detail.messages[1].data.id == first.data.id
    history = safe_support_history(detail.messages)
    assert all(message.data is None for message in history)
    snapshot = await chat.chart_data(actor(), first.data.id)
    assert snapshot.model_dump(mode="json")["rows"] == first.data.model_dump(mode="json")["rows"]


@pytest.mark.asyncio
async def test_chart_history_and_aggregate_scope_reauthorize(db):
    own, _ = await seed(db)
    chat = SupportConversationService(db)
    first = await chat.answer(
        question="Vẽ biểu đồ chi phí theo ngày trong 7 ngày", current_user=actor(), context=None, session_id=None
    )
    assert first.data.application_ids == [own.id]
    assert (await chat.chart_data(actor(), first.data.id)).total == pytest.approx(0.6)
    with pytest.raises(SupportChatNotFoundError):
        await chat.chart_data(actor("user-b"), first.data.id)
    with pytest.raises(SupportChatPermissionError):
        await chat.chart_data(actor(usage=False), first.data.id)
    revoked = await chat.get_session(actor(usage=False), first.session_id)
    assert revoked.messages[1].data is None
    own.owner_user_id = "user-b"
    await db.commit()
    with pytest.raises(SupportChatNotFoundError):
        await chat.chart_data(actor(), first.data.id)
    transferred = await chat.get_session(actor(), first.session_id)
    assert transferred.messages[1].data is None
    assert "0.600000" not in transferred.messages[1].content


@pytest.mark.asyncio
async def test_data_understanding_has_one_structured_call_and_no_embeddings():
    planner = SupportPlannerOutput(
        retrieval_query=None,
        intent="analytics",
        confidence=1,
        application_name="Alpha",
        period="last_7_days",
        follow_up=False,
        analytics=query(chart_type="line"),
    )
    client = AsyncMock()
    client.structured.return_value = planner
    service = SupportUnderstandingService(
        Settings(_env_file=None, support_chat_llm_enabled=True), client
    )
    result = await service.understand(
        SupportUnderstandingInput(
            question="Vẽ chi phí Alpha", documents=SupportKnowledgeService().documents("Vẽ chi phí Alpha")
        )
    )
    assert result.plan.intent == "analytics"
    client.structured.assert_awaited_once()
    client.embeddings.assert_not_awaited()


@pytest.mark.asyncio
async def test_analytics_does_not_allow_invented_dates_or_foreign_names(db):
    await seed(db)
    planner = SupportIntentPlan(
        intent="analytics",
        confidence=1,
        application_name="Alpha",
        period="today",
        follow_up=False,
        analytics=query(start_date="2026-01-01", end_date="2026-01-03"),
    )
    response = await SupportChatService(db).answer(question="Chi phí Alpha hôm nay", current_user=actor(), plan=planner)
    assert response.status == "needs_clarification" and response.data is None
    planner = planner.model_copy(update={"analytics": query(), "application_name": "Beta"})
    response = await SupportChatService(db).answer(
        question="Vẽ chi phí application Beta", current_user=actor(), plan=planner
    )
    assert response.status == "insufficient_evidence" and response.data is None


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-01", "2026-01-01"),
        ("2026-02-30", "2026-03-01"),
        ("2026-01-01", "2026-05-01"),
        ("2099-01-01", "2099-01-02"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_expensive_ranges_are_rejected(db, start, end):
    with pytest.raises(SupportDataQueryError):
        await SupportDataService(db).query(
            user_id="user-a", application=None, period="today", query=query(start_date=start, end_date=end)
        )


@pytest.mark.asyncio
async def test_truncated_breakdown_keeps_full_total(db):
    own, _ = await seed(db)
    db.add_all(
        [
            UsageEventRecord(
                request_id="own-1",
                application_id=own.id,
                model=f"extra-{index}",
                cost_usd=1,
                created_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            for index in range(105)
        ]
    )
    await db.commit()
    data = await SupportDataService(db).query(
        user_id="user-a", application=None, period="last_7_days", query=query("breakdown", group_by="model")
    )
    assert len(data.rows) == 100 and data.truncated
    assert data.total == pytest.approx(105.6)
    assert sum(row.value for row in data.rows) < data.total


@pytest.mark.asyncio
async def test_failure_table_contains_metadata_not_prompt_content(db):
    own, _ = await seed(db)
    record = await db.get(GatewayRequestRecord, "own-2")
    record.prompt = "PRIVATE PROMPT"
    record.response_json = {"answer": "PRIVATE RESPONSE"}
    await db.commit()
    data = await SupportDataService(db).query(
        user_id="user-a", application=own, period="last_7_days", query=query("failures", metric="requests", group_by="request")
    )
    assert data.unit == "ms"
    assert [(row.label, row.value) for row in data.rows] == [("own-2", 300)]
    assert "PRIVATE" not in data.model_dump_json()


@pytest.mark.asyncio
async def test_this_week_compares_same_weekday_progress(db, monkeypatch):
    import src.backend.services.support_data_service as module

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, 3, tzinfo=UTC).astimezone(tz or UTC)

    monkeypatch.setattr(module, "datetime", Frozen)
    app = Application(id=str(uuid4()), slug="week-app", name="Week", owner_user_id="user-a")
    db.add(app)
    await db.flush()
    for index, (when, cost) in enumerate(
        [
            (datetime(2026, 9, 28, 3, tzinfo=UTC), 2),
            (datetime(2026, 10, 3, 3, tzinfo=UTC), 99),
            (datetime(2026, 10, 6, 3, tzinfo=UTC), 4),
        ]
    ):
        request_id = f"week-{index}"
        db.add(GatewayRequestRecord(request_id=request_id, application_id=app.id, status="completed", started_at=when))
        await db.flush()
        db.add(UsageEventRecord(request_id=request_id, application_id=app.id, cost_usd=cost, created_at=when))
    await db.commit()
    data = await SupportDataService(db).query(
        user_id="user-a", application=app, period="this_week", query=query("compare")
    )
    assert data.total == 4 and data.baseline == 2 and data.percent_change == 100


def test_planner_schema_requires_all_fields_and_forbids_code():
    schema = SupportPlannerOutput.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    inner = schema["$defs"]["SupportAnalyticsQuery"]
    assert set(inner["required"]) == set(inner["properties"])
    assert inner["additionalProperties"] is False
    with pytest.raises(ValueError):
        query(chart_type="python")
    with pytest.raises(ValueError):
        SupportAnalyticsQuery.model_validate({**query().model_dump(), "code": "print('unsafe')"})
