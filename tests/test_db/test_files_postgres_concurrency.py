"""Run only against an explicitly isolated PostgreSQL database."""
import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.backend_ai.gateway.files.service import FileLimits, GatewayFileService, StorageQuotaExceededError
from src.backend_ai.gateway.files.storage import LocalFileStorage
from src.shared.models.audit import AuditEventRecord
from src.shared.models.core import Application
from src.shared.models.files import FileQuotaReservation, GatewayFileRecord


@pytest_asyncio.fixture
async def pg_file_fixture(tmp_path):
    from src.shared.models.files import FileDeletionOutbox
    url = os.getenv('OPENAI_AUDIT_ISOLATED_DATABASE_URL')
    if not url:
        pytest.skip('Explicit isolated PostgreSQL URL required')
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app_id = str(uuid4())
    async with factory() as session:
        session.add(Application(id=app_id,slug='file-recovery-'+uuid4().hex[:12],name='recovery',owner_user_id='fixture'))
        await session.commit()
    try:
        yield factory, LocalFileStorage(tmp_path/'files'), app_id
    finally:
        async with factory() as session:
            ids = list(await session.scalars(select(GatewayFileRecord.id).where(GatewayFileRecord.application_id == app_id)))
            await session.execute(delete(FileDeletionOutbox).where(FileDeletionOutbox.file_id.in_(ids)))
            await session.execute(delete(AuditEventRecord).where(AuditEventRecord.resource_id.in_(ids)))
            await session.execute(delete(FileQuotaReservation).where(FileQuotaReservation.application_id == app_id))
            await session.execute(delete(GatewayFileRecord).where(GatewayFileRecord.application_id == app_id))
            await session.execute(delete(Application).where(Application.id == app_id))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_two_workers_recover_crashed_upload_once(pg_file_fixture, monkeypatch):
    from datetime import timedelta

    from src.backend_ai.workers.file_maintenance import maintain_files
    from src.shared.models.core import utc_now
    factory, storage, app_id = pg_file_fixture
    reservation_id = 'file-' + uuid4().hex
    await storage.put(app_id+'/'+reservation_id,b'1234',content_type='text/plain')
    async with factory() as session:
        session.add(FileQuotaReservation(id=reservation_id,application_id=app_id,bytes=4,
            storage_backend='local',expires_at=utc_now()-timedelta(seconds=1)))
        await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    delete_object = storage.delete
    async def blocked(key):
        calls.append(key)
        entered.set()
        await release.wait()
        await delete_object(key)
    monkeypatch.setattr(storage,'delete',blocked)
    async with factory() as first, factory() as second:
        worker = asyncio.create_task(maintain_files(first,storage,FileLimits()))
        await asyncio.wait_for(entered.wait(),2)
        other = await maintain_files(second,storage,FileLimits())
        assert other['recovered'] == 0 and len(calls) == 1
        assert await second.scalar(select(func.sum(FileQuotaReservation.bytes)).where(
            FileQuotaReservation.application_id == app_id)) == 4
        await second.rollback()
        release.set()
        assert (await worker)['recovered'] == 1
    assert not (storage.root/app_id/reservation_id).exists()


@pytest.mark.asyncio
async def test_postgres_deletion_lease_survives_worker_restart(pg_file_fixture, monkeypatch):
    from datetime import timedelta

    from sqlalchemy import update

    from src.shared.models.core import utc_now
    from src.shared.models.files import FileDeletionOutbox
    factory, storage, app_id = pg_file_fixture
    original = storage.delete
    async def outage(key):
        raise ConnectionError('fixture outage')
    async with factory() as session:
        service = GatewayFileService(session,storage)
        record = await service.create(app_id,filename='a.txt',data=b'1234',declared_mime_type='text/plain')
        file_id = record.id
        monkeypatch.setattr(storage,'delete',outage)
        await service.delete(app_id,file_id)
        await session.execute(update(FileDeletionOutbox).where(FileDeletionOutbox.file_id == file_id)
            .values(lease_owner='dead-worker',lease_until=utc_now()+timedelta(minutes=1)))
        await session.commit()
    monkeypatch.setattr(storage,'delete',original)
    async with factory() as fresh:
        service = GatewayFileService(fresh,storage)
        assert await service.drain_deletions() == 0
        await fresh.execute(update(FileDeletionOutbox).where(FileDeletionOutbox.file_id == file_id)
            .values(lease_until=utc_now()-timedelta(seconds=1)))
        await fresh.commit()
        assert await service.drain_deletions() == 1
        assert (await fresh.get(GatewayFileRecord,file_id)).lifecycle == 'deleted'
        assert await fresh.get(FileDeletionOutbox,file_id) is None


@pytest.mark.asyncio
async def test_postgres_upload_reservations_prevent_two_writers_exceeding_quota(tmp_path):
    url = os.getenv('OPENAI_AUDIT_ISOLATED_DATABASE_URL')
    if not url:
        pytest.skip('Requires explicit isolated PostgreSQL audit URL; never falls back to .env')
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app_id = str(uuid4())
    storage = LocalFileStorage(tmp_path / 'files')
    try:
        async with factory() as session:
            session.add(Application(id=app_id, slug='quota-' + uuid4().hex[:12], name='quota fixture', owner_user_id='fixture'))
            await session.commit()
        async def upload(name):
            async with factory() as session:
                service = GatewayFileService(session, storage, FileLimits(application_quota_bytes=5))
                return await service.create(app_id, filename=name, data=b'1234', declared_mime_type='text/plain')
        results = await asyncio.gather(upload('a.txt'), upload('b.txt'), return_exceptions=True)
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        assert sum(isinstance(result, StorageQuotaExceededError) for result in results) == 1
        async with factory() as session:
            assert await session.scalar(select(func.sum(GatewayFileRecord.bytes)).where(GatewayFileRecord.application_id == app_id)) == 4
            assert await session.scalar(select(func.count()).select_from(FileQuotaReservation).where(FileQuotaReservation.application_id == app_id)) == 0
    finally:
        async with factory() as session:
            ids = list(await session.scalars(select(GatewayFileRecord.id).where(GatewayFileRecord.application_id == app_id)))
            await session.execute(delete(AuditEventRecord).where(AuditEventRecord.resource_id.in_(ids)))
            await session.execute(delete(FileQuotaReservation).where(FileQuotaReservation.application_id == app_id))
            await session.execute(delete(GatewayFileRecord).where(GatewayFileRecord.application_id == app_id))
            await session.execute(delete(Application).where(Application.id == app_id))
            await session.commit()
        await engine.dispose()
