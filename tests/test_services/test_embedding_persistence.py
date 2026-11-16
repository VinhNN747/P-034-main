from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.backend_ai.gateway.providers.embeddings import EmbeddingProviderResult
from src.backend_ai.services import embedding_persistence
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.shared.dtos.runtime_api import RuntimeRequestResponse, RuntimeUsage, RuntimeUsageScope


class FakeSession:
    def __init__(self):
        self.records = []
        self.flushed = 0
        self.committed = 0

    def add(self, record):
        self.records.append(record)

    async def flush(self):
        self.flushed += 1


class FakeBilling:
    def __init__(self, _session):
        pass

    async def debit_runtime_request(self, application_id, request_id, amount, status):
        captured.update(application_id=application_id, request_id=request_id, amount=amount, status=status)
        return SimpleNamespace(id="debit-id") if status == "completed" else None


captured = {}


@pytest.mark.asyncio
async def test_embedding_persists_scalar_usage_without_vector_or_input(monkeypatch):
    captured.clear()
    monkeypatch.setattr(embedding_persistence, "BillingService", FakeBilling)
    session = FakeSession()
    started = datetime.now(UTC)
    result = EmbeddingProviderResult("text-embedding-3-small", [[0.1, 0.2]], 10, 3.5)
    scope = RuntimeUsageScope(input_tokens=10, output_tokens=0, total_tokens=10)
    receipt = RuntimeRequestResponse(
        request_id="op-1", application_id="app-1", status="completed", execution_mode="live",
        usage=RuntimeUsage(execution=scope, pipeline=scope, complete=True),
        config_version=7, started_at=started, finished_at=started,
    )

    await embedding_persistence.EmbeddingPersistence(session).stage_run(
        ApplicationPrincipal("app-1", "key-1"), "op-1", "model-1", "openai", result, receipt,
        input_count=1, dimensions=2, input_price_per_million=Decimal("2"), config_version=7,
        started_at=started, finished_at=started,
    )

    request_row = session.records[0]
    execution_row = session.records[1]
    usage_row = session.records[2]
    assert request_row.source == "openai_embeddings"
    assert request_row.prompt is None and request_row.messages_json == []
    assert request_row.aggregation_status is None
    assert "input" not in request_row.response_json and "vectors" not in request_row.response_json
    assert request_row.total_cost == 0.00002
    assert execution_row.input_tokens == 10 and execution_row.output_tokens == 0
    assert execution_row.model == "model-1" and execution_row.result_payload is None
    assert usage_row.source == "openai_embeddings"
    assert usage_row.billing_transaction_id == "debit-id"
    assert request_row.billing_transaction_id == "debit-id"
    assert captured["amount"] == Decimal("0.00002") and captured["status"] == "completed"
    assert session.committed == 0


@pytest.mark.asyncio
async def test_failed_embedding_never_requests_a_debit(monkeypatch):
    captured.clear()
    monkeypatch.setattr(embedding_persistence, "BillingService", FakeBilling)
    session = FakeSession()
    now = datetime.now(UTC)
    receipt = RuntimeRequestResponse(
        request_id="op-fail", application_id="app-1", status="failed", execution_mode="live",
        config_version=1, started_at=now, finished_at=now,
    )
    await embedding_persistence.EmbeddingPersistence(session).stage_run(
        ApplicationPrincipal("app-1", "key-1"), "op-fail", "model-1", "openai", None, receipt,
        input_count=1, dimensions=1536, input_price_per_million=Decimal("2"), config_version=1,
        started_at=now, finished_at=now, error_code="embedding_provider_unavailable",
    )
    assert captured["status"] == "failed"
    assert captured["amount"] == Decimal("0")
    assert not any(type(row).__name__ == "UsageEventRecord" for row in session.records)
