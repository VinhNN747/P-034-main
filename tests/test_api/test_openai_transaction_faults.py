"""Isolated DB connection and transaction acknowledgement faults."""
import socket
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.shared.database.session import get_db
from src.shared.models.billing import BillingTransactionRecord
from src.shared.models.core import utc_now
from src.shared.models.gateway import Model
from src.shared.models.runtime import RuntimeOperationRecord
from src.shared.services.gateway_persistence_service import GatewayPersistenceService
from tests.service_app import app
from tests.test_api.test_openai_compat import openai_env as openai_env


@pytest.mark.asyncio
@pytest.mark.parametrize('ack_lost', [False, True])
async def test_runtime_commit_fault_never_reexecutes_or_debits_twice(openai_env, monkeypatch, ack_lost):
    async with openai_env['factory']() as session:
        await session.execute(update(Model).values(input_price_per_million=1.0, output_price_per_million=1.0))
        await session.commit()
    calls = []
    original_generate, original_stage, original_commit = OfflineProvider.generate, GatewayPersistenceService.stage_run, AsyncSession.commit
    async def generate(self, *args, **kwargs):
        calls.append(True)
        return await original_generate(self, *args, **kwargs)
    async def stage(self, *args, **kwargs):
        result = await original_stage(self, *args, **kwargs)
        self.session.info['audit_fail_commit'] = True
        return result
    async def commit(self):
        if self.info.pop('audit_fail_commit', False):
            if ack_lost:
                await original_commit(self)
            raise OperationalError('commit', {}, ConnectionError('private-db-address'))
        return await original_commit(self)
    monkeypatch.setattr(OfflineProvider, 'generate', generate)
    monkeypatch.setattr(GatewayPersistenceService, 'stage_run', stage)
    monkeypatch.setattr(AsyncSession, 'commit', commit)
    body = {'model': 'auto:fast', 'messages': [{'role': 'user', 'content': 'Hello'}]}
    headers = {'Authorization': 'Bearer ' + openai_env['key'], 'Idempotency-Key': 'commit-fault'}
    first = await openai_env['client'].post('/v1/chat/completions', json=body, headers=headers)
    assert first.status_code == 503 and first.headers['x-request-id']
    assert 'private-db-address' not in first.text
    replay = await openai_env['client'].post('/v1/chat/completions', json=body, headers=headers)
    assert replay.status_code == (200 if ack_lost else 409), replay.text
    assert len(calls) == 1
    async with openai_env['factory']() as session:
        operation = await session.scalar(select(RuntimeOperationRecord))
        debit_count = await session.scalar(select(func.count()).select_from(BillingTransactionRecord).where(
            BillingTransactionRecord.source_request_id == operation.request_id))
        assert debit_count == (1 if ack_lost else 0)
        if not ack_lost:
            await session.execute(update(RuntimeOperationRecord).where(RuntimeOperationRecord.request_id == operation.request_id)
                                  .values(expires_at=utc_now() - timedelta(seconds=1)))
            await session.commit()
    if not ack_lost:
        retry = await openai_env['client'].post('/v1/chat/completions', json=body, headers=headers)
        assert retry.status_code == 409 and retry.json()['error']['code'] == 'request_outcome_unknown'
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_database_tcp_outage_is_correlated_and_recovers_without_generation(openai_env):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        unavailable_port = probe.getsockname()[1]
    engine = create_async_engine(f'postgresql+psycopg://fixture:fixture@127.0.0.1:{unavailable_port}/fixture',
                                 connect_args={'connect_timeout': 1})
    factory = async_sessionmaker(engine)
    original = app.dependency_overrides[get_db]
    async def unavailable_db():
        async with factory() as session:
            yield session
    app.dependency_overrides[get_db] = unavailable_db
    try:
        response = await openai_env['client'].get('/v1/models', headers={'Authorization': 'Bearer ' + openai_env['key']})
        assert response.status_code >= 500 and response.headers['x-request-id']
        assert response.json()['error']['type'] == 'server_error'
        assert 'postgresql' not in response.text and 'fixture:fixture' not in response.text
    finally:
        app.dependency_overrides[get_db] = original
        await engine.dispose()
    recovered = await openai_env['client'].get('/v1/models', headers={'Authorization': 'Bearer ' + openai_env['key']})
    assert recovered.status_code == 200
