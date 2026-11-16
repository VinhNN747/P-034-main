from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.database.base import Base
from src.shared.database.session import get_db
from src.shared.dependencies.auth import CurrentUser, get_current_user
from src.shared.models.audit import AuditEventRecord
from src.shared.models.core import Application
from src.shared.models.integration import PolicyReviewRecord
from src.shared.models.runtime import GatewayRequestRecord, IncidentRecord
from tests.service_app import app


@pytest_asyncio.fixture
async def task8_client(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "task8.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    permissions: set[str] = {"incidents.read", "incidents.manage", "routing_policies.read", "routing_policies.manage"}
    application_id = str(uuid4())
    async with factory() as session:
        session.add(
            Application(
                id=application_id,
                slug="task8-app",
                name="Task 8 App",
                owner_user_id="user-1",
                environment="production",
                status="active",
            )
        )
        session.add_all(
            [
                PolicyReviewRecord(
                    id="PR-001",
                    application_id=application_id,
                    name="Prefer the fast model",
                    scope="Task 8 App",
                    review_type="routing",
                    submitted_by="optimizer",
                    affected_models_json=["model-fast"],
                    metadata_json={"affected_applications": [application_id], "reason": "Lower latency"},
                ),
                PolicyReviewRecord(
                    id="PR-002",
                    application_id=application_id,
                    name="Raise quality floor",
                    scope="Task 8 App",
                    review_type="guardrail",
                    submitted_by="operator",
                    affected_models_json=["model-quality"],
                    metadata_json={"affected_applications": [application_id]},
                ),
                PolicyReviewRecord(
                    id="PR-003",
                    application_id=application_id,
                    name="Retire degraded fallback",
                    scope="Task 8 App",
                    review_type="routing",
                    submitted_by="optimizer",
                    affected_models_json=["model-degraded"],
                    metadata_json={"affected_applications": [application_id]},
                ),
            ]
        )
        await session.commit()

    async def override_db():
        async with factory() as session:
            yield session

    async def override_user():
        return CurrentUser(
            id="user-1",
            email="user@example.com",
            roles=("operator",),
            permissions=frozenset(permissions),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, permissions, application_id, factory
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_incidents_filter_create_patch_status_transition_and_audit(task8_client):
    client, _, application_id, factory = task8_client
    created = await client.post(
        "/api/v1/gateway/incidents",
        json={
            "application_id": application_id,
            "title": "Provider latency increased",
            "description": "p95 crossed the configured threshold",
            "severity": "high",
            "status": "investigating",
            "owner": "Platform Ops",
            "type": "provider_outage",
            "scope": "Offline provider",
            "impact": "Requests may fall back",
            "provider": "offline",
            "model": "local-deterministic",
        },
    )
    assert created.status_code == 201, created.text
    incident = created.json()
    assert incident["severity"] == "high"
    assert incident["status"] == "investigating"
    assert incident["owner"] == "Platform Ops"
    assert incident["type"] == "provider_outage"
    assert incident["scope"] == "Offline provider"
    assert incident["impact"] == "Requests may fall back"
    assert incident["opened_at"]
    assert incident["duration_seconds"] >= 0

    filtered = await client.get(
        "/api/v1/gateway/incidents",
        params={"severity": "high", "status": "investigating", "application_id": application_id},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["id"] == incident["id"]

    monitoring = await client.patch(
        f"/api/v1/gateway/incidents/{incident['id']}",
        json={"status": "monitoring"},
    )
    assert monitoring.status_code == 200
    assert monitoring.json()["status"] == "monitoring"
    resolved = await client.patch(
        f"/api/v1/gateway/incidents/{incident['id']}",
        json={"status": "resolved"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"

    async with factory() as session:
        audit = list(
            await session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.resource_type == "incident",
                    AuditEventRecord.resource_id == incident["id"],
                )
            )
        )
    assert [event.action for event in audit] == ["incident.created", "incident.updated", "incident.updated"]
    assert all(event.actor_user_id == "user-1" for event in audit)


@pytest.mark.asyncio
async def test_policy_reviews_list_and_decision_history_for_all_decisions(task8_client):
    client, _, application_id, factory = task8_client
    listed = await client.get("/api/v1/policy-reviews", params={"application_id": application_id, "status": "pending"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 3
    assert listed.json()["items"][0]["affected_applications"] == [application_id]

    for review_id, decision, expected_status in (
        ("PR-001", "approve", "approved"),
        ("PR-002", "reject", "rejected"),
        ("PR-003", "request_changes", "changes_requested"),
    ):
        response = await client.post(
            f"/api/v1/policy-reviews/{review_id}/decision",
            json={"decision": decision, "comment": f"Decision: {decision}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == expected_status
        assert body["decision_history"][-1]["actor_user_id"] == "user-1"
        assert body["decision_history"][-1]["decision"] == decision

    reviewed = await client.get("/api/v1/policy-reviews", params={"status": "approved"})
    assert reviewed.status_code == 200
    assert [item["id"] for item in reviewed.json()["items"]] == ["PR-001"]

    async with factory() as session:
        audit = list(
            await session.scalars(select(AuditEventRecord).where(AuditEventRecord.resource_type == "policy_review"))
        )
    assert len(audit) == 3
    assert {event.action for event in audit} == {
        "policy_review.approved",
        "policy_review.rejected",
        "policy_review.changes_requested",
    }


@pytest.mark.asyncio
async def test_incident_and_policy_mutations_require_existing_permissions(task8_client):
    client, permissions, application_id, _ = task8_client
    permissions.clear()
    denied_incident = await client.post(
        "/api/v1/gateway/incidents",
        json={"application_id": application_id, "title": "Denied", "severity": "low"},
    )
    denied_policy = await client.post(
        "/api/v1/policy-reviews/PR-001/decision",
        json={"decision": "approve", "comment": "Denied"},
    )
    assert denied_incident.status_code == 403
    assert denied_policy.status_code == 403

    permissions.add("routing_policies.read")
    denied_read_decision = await client.post(
        "/api/v1/policy-reviews/PR-001/decision",
        json={"decision": "approve", "comment": "Still denied"},
    )
    assert denied_read_decision.status_code == 403


def use_incident_identity(user_id: str, permissions: set[str] | None = None):
    async def override_user():
        return CurrentUser(
            id=user_id, email=f"{user_id}@example.com", roles=("user",),
            permissions=frozenset(permissions or set()),
        )
    app.dependency_overrides[get_current_user] = override_user


@pytest.mark.asyncio
async def test_user_report_is_persisted_and_admin_can_handle_another_users_ticket(task8_client):
    client, _, application_id, factory = task8_client
    use_incident_identity("user-1")
    created = await client.post("/api/v1/gateway/incident-reports", json={
        "application_id": application_id, "title": "  Fallback did not run  ",
        "description": "The selected model failed and the request stopped.", "type": "fallback",
        "model": "model-fast", "provider": "test-provider",
        "steps_to_reproduce": "Send a request while the primary model is unavailable.",
        "expected_behavior": "The configured fallback should answer.",
    })
    assert created.status_code == 201, created.text
    ticket = created.json()
    assert ticket["title"] == "Fallback did not run"
    assert ticket["source"] == "user_report"
    assert ticket["reporter_user_id"] == "user-1"
    assert ticket["reporter_email"] == "user-1@example.com"
    assert ticket["severity"] == "medium"
    assert ticket["status"] == "investigating"
    assert ticket["owner"] == "Unassigned"
    assert (await client.get("/api/v1/gateway/incidents")).status_code == 403
    assert (await client.patch(f"/api/v1/gateway/incidents/{ticket['id']}", json={"status": "resolved"})).status_code == 403

    async with factory() as session:
        stored = await session.get(IncidentRecord, ticket["id"])
        assert stored.description == ticket["description"]
        assert stored.metadata_json["expected_behavior"] == ticket["expected_behavior"]
        assert stored.metadata_json["reporter_user_id"] == "user-1"

    use_incident_identity("admin-other", {"incidents.read", "incidents.manage"})
    admin_list = await client.get("/api/v1/gateway/incidents", params={"search": "Fallback"})
    assert admin_list.status_code == 200, admin_list.text
    assert [item["id"] for item in admin_list.json()["items"]] == [ticket["id"]]
    for state in ("monitoring", "resolved"):
        updated = await client.patch(f"/api/v1/gateway/incidents/{ticket['id']}", json={
            "status": state, "severity": "high", "owner": "Platform support",
            "resolution_note": "Fixed fallback routing and verified the request.",
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == state
        assert updated.json()["reporter_user_id"] == "user-1"

    use_incident_identity("user-1")
    own = await client.get("/api/v1/gateway/incident-reports", params={"status": "resolved", "severity": "high"})
    assert own.status_code == 200
    assert own.json()["total"] == 1
    assert own.json()["items"][0]["resolution_note"] == "Fixed fallback routing and verified the request."
    async with factory() as session:
        audit = list(await session.scalars(select(AuditEventRecord).where(AuditEventRecord.resource_id == ticket["id"])))
    assert [event.action for event in audit] == ["incident.reported", "incident.updated", "incident.updated"]
    assert [event.actor_user_id for event in audit] == ["user-1", "admin-other", "admin-other"]


@pytest.mark.asyncio
async def test_general_reports_are_private_and_paginated(task8_client):
    client, _, application_id, _ = task8_client
    use_incident_identity("user-1")
    tickets = []
    for index in range(3):
        response = await client.post("/api/v1/gateway/incident-reports", json={
            "title": f"Logic issue {index}", "description": "Unexpected answer", "type": "logic",
            **({"application_id": application_id} if index == 0 else {}),
        })
        assert response.status_code == 201, response.text
        tickets.append(response.json()["id"])
    page = await client.get("/api/v1/gateway/incident-reports", params={"page": 2, "page_size": 2, "search": "Logic"})
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 1

    use_incident_identity("user-2")
    other = await client.get("/api/v1/gateway/incident-reports")
    assert other.status_code == 200
    assert other.json()["total"] == 0
    response = await client.post("/api/v1/gateway/incident-reports", json={"title": "Other user", "description": "An issue"})
    assert response.status_code == 201
    use_incident_identity("user-1")
    own = await client.get("/api/v1/gateway/incident-reports")
    assert {item["id"] for item in own.json()["items"]} == set(tickets)


@pytest.mark.asyncio
async def test_reports_reject_foreign_applications_requests_and_mismatched_context(task8_client):
    client, _, application_id, factory = task8_client
    other_application_id = str(uuid4())
    async with factory() as session:
        session.add(Application(id=other_application_id, slug="other-app", name="Other", owner_user_id="user-2"))
        session.add_all([
            GatewayRequestRecord(request_id="own-request", application_id=application_id, status="failed"),
            GatewayRequestRecord(request_id="foreign-request", application_id=other_application_id, status="failed"),
        ])
        await session.commit()
    use_incident_identity("user-1")
    for context in (
        {"application_id": other_application_id}, {"request_id": "foreign-request"},
        {"request_id": "missing-request"}, {"application_id": "missing-app"},
    ):
        result = await client.post("/api/v1/gateway/incident-reports", json={"title": "Issue", "description": "Failure", **context})
        assert result.status_code == 404, result.text
    own = await client.post("/api/v1/gateway/incident-reports", json={"title": "Issue", "description": "Failure", "request_id": "own-request"})
    assert own.status_code == 201, own.text
    assert own.json()["application_id"] == application_id
    assert own.json()["request_id"] == "own-request"

    use_incident_identity("user-2")
    mismatch = await client.post("/api/v1/gateway/incident-reports", json={
        "title": "Issue", "description": "Failure", "application_id": other_application_id, "request_id": "own-request",
    })
    assert mismatch.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"title": "   "}, {"description": " \n "}, {"title": "x" * 201}, {"description": "x" * 10001},
    {"type": "unknown"}, {"status": "resolved"}, {"severity": "critical"},
    {"reporter_user_id": "admin"}, {"owner": "admin"}, {"metadata": {"source": "operational"}},
])
async def test_report_input_validation_and_server_owned_fields(task8_client, extra):
    client, _, _, _ = task8_client
    use_incident_identity("user-1")
    response = await client.post("/api/v1/gateway/incident-reports", json={"title": "Issue", "description": "Failure", **extra})
    assert response.status_code == 422, response.text
    assert (await client.get("/api/v1/gateway/incident-reports")).json()["total"] == 0


@pytest.mark.asyncio
async def test_report_routes_require_authentication(task8_client):
    client, _, _, _ = task8_client
    app.dependency_overrides.pop(get_current_user)
    assert (await client.get("/api/v1/gateway/incident-reports")).status_code == 401
    assert (await client.post("/api/v1/gateway/incident-reports", json={"title": "Issue", "description": "Failure"})).status_code == 401


@pytest.mark.asyncio
async def test_admin_can_handle_ticket_after_application_deletion_and_readers_cannot_mutate(task8_client):
    client, _, application_id, factory = task8_client
    use_incident_identity("user-1")
    created = await client.post("/api/v1/gateway/incident-reports", json={
        "title": "Issue", "description": "Failure", "application_id": application_id,
    })
    ticket = created.json()
    async with factory() as session:
        await session.execute(delete(Application).where(Application.id == application_id))
        await session.commit()
    use_incident_identity("admin-reader", {"incidents.read"})
    assert (await client.get("/api/v1/gateway/incidents")).json()["total"] == 1
    assert (await client.patch(f"/api/v1/gateway/incidents/{ticket['id']}", json={"status": "resolved"})).status_code == 403
    use_incident_identity("admin-manager", {"incidents.manage"})
    assert (await client.patch(f"/api/v1/gateway/incidents/{ticket['id']}", json={"status": None})).status_code == 422
    updated = await client.patch(f"/api/v1/gateway/incidents/{ticket['id']}", json={"status": "resolved"})
    assert updated.status_code == 200, updated.text
    use_incident_identity("user-1")
    assert (await client.get("/api/v1/gateway/incident-reports")).json()["items"][0]["status"] == "resolved"
