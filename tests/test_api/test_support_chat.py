from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.billing import BillingAccountRecord
from src.shared.models.core import Application
from src.shared.models.support_chat import SupportChatMessageRecord
from tests.service_app import app


@pytest_asyncio.fixture
async def support_database(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "support-router.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def current_user(*permissions: str) -> CurrentUser:
    return CurrentUser(
        id="user-a",
        email="user-a@example.com",
        roles=("ai_engineer",),
        permissions=frozenset(permissions),
    )


@pytest.mark.asyncio
async def test_support_chat_blocks_a_sensitive_tool_without_its_permission(client, support_database):
    async def override_user():
        return current_user("applications.read_own")

    async def override_db():
        async with support_database() as session:
            yield session

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        response = await client.post("/api/v1/support/chat", json={"message": "Số dư credit của tôi?"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"] == "Insufficient permission for the requested support data"


@pytest.mark.asyncio
async def test_support_chat_does_not_allow_context_for_another_users_application(client, support_database):
    application_id = str(uuid4())
    async with support_database() as session:
        session.add(
            Application(
                id=application_id,
                slug="other-user-application",
                name="Other user application",
                owner_user_id="user-b",
                environment="development",
                status="active",
            )
        )
        await session.commit()

    async def override_user():
        return current_user("applications.read_own", "usage.read_own")

    async def override_db():
        async with support_database() as session:
            yield session

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        response = await client.post(
            "/api/v1/support/chat",
            json={
                "message": "Usage hôm nay là bao nhiêu token?",
                "context": {"page": "application_detail", "application_id": application_id},
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "Application not found"


@pytest.mark.asyncio
async def test_support_history_is_scoped_to_authenticated_user_and_survives_new_session(client, support_database):
    actor = current_user("applications.read_own")

    async def override_user():
        return actor

    async def override_db():
        async with support_database() as session:
            yield session

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        first = await client.post("/api/v1/support/chat", json={"message": "Liệt kê application của tôi"})
        assert first.status_code == 200
        first_id = first.json()["session_id"]
        continued = await client.post(
            "/api/v1/support/chat",
            json={"session_id": first_id, "message": "Hướng dẫn tạo application"},
        )
        assert continued.status_code == 200
        assert continued.json()["session_id"] == first_id

        new = await client.post("/api/v1/support/sessions")
        assert new.status_code == 201
        assert new.json()["id"] != first_id

        history = await client.get(f"/api/v1/support/sessions/{first_id}")
        assert history.status_code == 200
        assert history.headers["cache-control"] == "private, no-store"
        assert [item["role"] for item in history.json()["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert history.json()["messages"][0]["content"] == "Liệt kê application của tôi"
        recent = await client.get(f"/api/v1/support/sessions/{first_id}?limit=2")
        older = await client.get(f"/api/v1/support/sessions/{first_id}?limit=2&offset=2")
        assert recent.json()["has_more"] is True
        assert older.json()["has_more"] is False
        assert recent.json()["messages"][0]["id"] == history.json()["messages"][2]["id"]
        assert older.json()["messages"][0]["id"] == history.json()["messages"][0]["id"]

        actor = CurrentUser(
            id="user-b",
            email="user-b@example.com",
            roles=("ai_engineer",),
            permissions=frozenset({"applications.read_own"}),
        )
        assert (await client.get(f"/api/v1/support/sessions/{first_id}")).status_code == 404
        assert (
            await client.post(
                "/api/v1/support/chat",
                json={"session_id": first_id, "message": "Hướng dẫn tạo application"},
            )
        ).status_code == 404
        assert (await client.get("/api/v1/support/sessions")).json()["items"] == []
        async with support_database() as session:
            count = await session.scalar(select(func.count()).select_from(SupportChatMessageRecord))
            assert count == 4
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_history_masks_billing_answer_after_permission_revocation(client, support_database):
    actor = current_user("applications.read_own", "billing.read")
    async with support_database() as session:
        session.add(BillingAccountRecord(id=str(uuid4()), user_id="user-a", balance=12.5, currency="USD"))
        await session.commit()

    async def override_user():
        return actor

    async def override_db():
        async with support_database() as session:
            yield session

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        first = await client.post("/api/v1/support/chat", json={"message": "Số dư credit của tôi?"})
        assert first.status_code == 200
        session_id = first.json()["session_id"]
        actor = current_user("applications.read_own")
        history = await client.get(f"/api/v1/support/sessions/{session_id}")
        assert history.status_code == 200
        answer = history.json()["messages"][1]
        assert "12.5" not in answer["content"]
        assert answer["sources"] == []
        assert answer["actions"] == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_private_highcharts_data_roundtrip_and_revocation(client, support_database):
    from datetime import UTC, datetime, timedelta

    from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord

    actor = current_user("applications.read_own", "usage.read_own")
    application_id = str(uuid4())
    async with support_database() as session:
        session.add(Application(id=application_id, slug="chart-alpha", name="Alpha", owner_user_id="user-a"))
        await session.flush()
        session.add(
            GatewayRequestRecord(
                request_id="chart-request",
                application_id=application_id,
                status="completed",
                started_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await session.flush()
        session.add(
            UsageEventRecord(
                request_id="chart-request",
                application_id=application_id,
                cost_usd=0.25,
                created_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await session.commit()

    async def override_user():
        return actor

    async def override_db():
        async with support_database() as session:
            yield session

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        answer = await client.post(
            "/api/v1/support/chat", json={"message": "Vẽ biểu đồ chi phí application Alpha theo ngày"}
        )
        assert answer.status_code == 200
        data = answer.json()["data"]
        assert data["total"] == 0.25
        assert "application_ids" not in data
        chart_url = f"/api/v1/support/charts/{data['id']}"
        chart = await client.get(chart_url)
        assert chart.status_code == 200
        assert chart.headers["content-type"] == "application/json"
        assert chart.headers["cache-control"] == "private, no-store"
        assert chart.json()["rows"] == data["rows"]
        assert chart.json()["total"] == 0.25
        assert "application_ids" not in chart.json()

        actor = current_user("applications.read_own")
        assert (await client.get(chart_url)).status_code == 403
        history = await client.get(f"/api/v1/support/sessions/{answer.json()['session_id']}")
        assert history.json()["messages"][1]["data"] is None

        actor = CurrentUser(
            id="user-b", email=None, roles=("user",), permissions=frozenset({"applications.read_own", "usage.read_own"})
        )
        assert (await client.get(chart_url)).status_code == 404

        actor = current_user("applications.read_own", "usage.read_own")
        deleted = await client.delete(f"/api/v1/support/sessions/{answer.json()['session_id']}")
        assert deleted.status_code == 204
        # Saved chart data cannot be fetched after the session is deleted.
        assert (await client.get(chart_url)).status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_chart_requires_authentication(client):
    response = await client.get(f"/api/v1/support/charts/{uuid4()}")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_recovery_session_cannot_read_or_modify_chat_before_completing_login(client, support_database):
    actor = CurrentUser(
        id="user-a", email="user-a@example.com", roles=("user",),
        permissions=frozenset({"applications.read_own", "usage.read_own"}),
    )

    async def override_user():
        return actor

    async def override_db():
        async with support_database() as session:
            yield session

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        created = await client.post("/api/v1/support/sessions")
        assert created.status_code == 201
        session_id = created.json()["id"]
        actor = CurrentUser(
            id="user-a", email="user-a@example.com", roles=("user",),
            permissions=frozenset({"applications.read_own", "usage.read_own"}), authentication_complete=False,
        )
        assert (await client.get("/api/v1/support/sessions")).status_code == 403
        assert (await client.get(f"/api/v1/support/sessions/{session_id}")).status_code == 403
        assert (await client.post("/api/v1/support/sessions")).status_code == 403
        assert (await client.patch(f"/api/v1/support/sessions/{session_id}", json={"pinned": True})).status_code == 403
        assert (await client.delete(f"/api/v1/support/sessions/{session_id}")).status_code == 403
        assert (await client.post("/api/v1/support/chat", json={"message": "Help", "session_id": session_id})).status_code == 403
        assert (await client.get(f"/api/v1/support/charts/{uuid4()}")).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_delete_session_removes_messages_snapshots_and_keeps_other_sessions(client, support_database):
    from datetime import UTC, datetime

    from src.shared.models.support_chat import SupportChatRateLimitRecord, SupportChatSessionRecord

    owned_id, retained_id, foreign_id = (str(uuid4()) for _ in range(3))
    chart_id = str(uuid4())
    async with support_database() as session:
        session.add_all(
            [
                SupportChatSessionRecord(id=owned_id, user_id="user-a"),
                SupportChatSessionRecord(id=retained_id, user_id="user-a"),
                SupportChatSessionRecord(id=foreign_id, user_id="user-b"),
                SupportChatRateLimitRecord(user_id="user-a", window_started_at=datetime.now(UTC), request_count=5),
            ]
        )
        await session.flush()
        session.add_all(
            [
                SupportChatMessageRecord(session_id=owned_id, user_id="user-a", role="user", content="Question"),
                SupportChatMessageRecord(
                    id=chart_id,
                    session_id=owned_id,
                    user_id="user-a",
                    role="assistant",
                    content="Chart",
                    data_json={"result": {"id": chart_id}},
                ),
                SupportChatMessageRecord(session_id=retained_id, user_id="user-a", role="user", content="Keep owned"),
                SupportChatMessageRecord(session_id=foreign_id, user_id="user-b", role="user", content="Keep foreign"),
            ]
        )
        await session.commit()

    async def override_user():
        return current_user("applications.read_own", "usage.read_own")

    async def override_db():
        async with support_database() as session:
            yield session

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        response = await client.delete(f"/api/v1/support/sessions/{owned_id}")
        assert response.status_code == 204
        assert response.content == b""
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get(f"/api/v1/support/sessions/{owned_id}")).status_code == 404
        assert (await client.get(f"/api/v1/support/charts/{chart_id}")).status_code == 404
        assert (await client.delete(f"/api/v1/support/sessions/{owned_id}")).status_code == 404
        listed = await client.get("/api/v1/support/sessions")
        assert {item["id"] for item in listed.json()["items"]} == {retained_id}
        async with support_database() as session:
            assert await session.get(SupportChatSessionRecord, owned_id) is None
            assert set(await session.scalars(select(SupportChatMessageRecord.content))) == {
                "Keep owned",
                "Keep foreign",
            }
            assert (await session.get(SupportChatRateLimitRecord, "user-a")).request_count == 5
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_session_does_not_allow_foreign_or_unauthorized_deletion(client, support_database):
    from src.shared.models.support_chat import SupportChatSessionRecord

    owned_id, foreign_id = str(uuid4()), str(uuid4())
    async with support_database() as session:
        session.add_all(
            [
                SupportChatSessionRecord(id=owned_id, user_id="user-a"),
                SupportChatSessionRecord(id=foreign_id, user_id="user-b"),
            ]
        )
        await session.commit()
    actor = current_user("applications.read_own")

    async def override_user():
        return actor

    async def override_db():
        async with support_database() as session:
            yield session

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        assert (await client.delete(f"/api/v1/support/sessions/{foreign_id}")).status_code == 404
        assert (await client.delete(f"/api/v1/support/sessions/{uuid4()}")).status_code == 404
        actor = current_user()
        assert (await client.delete(f"/api/v1/support/sessions/{owned_id}")).status_code == 403
        actor = CurrentUser(id="user-a", email=None, roles=("admin",), permissions=frozenset({"applications.read_own"}))
        assert (await client.delete(f"/api/v1/support/sessions/{owned_id}")).status_code == 403
        app.dependency_overrides.pop(get_current_user)
        assert (await client.delete(f"/api/v1/support/sessions/{owned_id}")).status_code == 401
        async with support_database() as session:
            assert await session.get(SupportChatSessionRecord, owned_id) is not None
            assert await session.get(SupportChatSessionRecord, foreign_id) is not None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_session_can_be_renamed_and_pinned_but_only_by_its_owner(client, support_database):
    from src.shared.models.support_chat import SupportChatSessionRecord

    pinned_id, recent_id, foreign_id = str(uuid4()), str(uuid4()), str(uuid4())
    now = datetime.now(UTC)
    async with support_database() as session:
        session.add_all(
            [
                SupportChatSessionRecord(
                    id=pinned_id,
                    user_id="user-a",
                    title="Older conversation",
                    updated_at=now - timedelta(days=2),
                ),
                SupportChatSessionRecord(id=recent_id, user_id="user-a", title="Recent conversation", updated_at=now),
                SupportChatSessionRecord(id=foreign_id, user_id="user-b", title="Foreign conversation", updated_at=now),
            ]
        )
        await session.commit()

    async def override_user():
        return current_user("applications.read_own")

    async def override_db():
        async with support_database() as session:
            yield session

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        updated = await client.patch(
            f"/api/v1/support/sessions/{pinned_id}",
            json={"title": "  Important   Gateway notes  ", "pinned": True},
        )
        assert updated.status_code == 200
        assert updated.json()["title"] == "Important Gateway notes"
        assert updated.json()["pinned"] is True

        listed = await client.get("/api/v1/support/sessions")
        assert [item["id"] for item in listed.json()["items"]] == [pinned_id, recent_id]
        assert listed.json()["items"][0]["pinned"] is True

        unpinned = await client.patch(f"/api/v1/support/sessions/{pinned_id}", json={"pinned": False})
        assert unpinned.status_code == 200
        assert unpinned.json()["pinned"] is False
        assert (await client.patch(f"/api/v1/support/sessions/{pinned_id}", json={"title": "   "})).status_code == 422
        assert (await client.patch(f"/api/v1/support/sessions/{foreign_id}", json={"pinned": True})).status_code == 404
    finally:
        app.dependency_overrides.clear()
