from datetime import timedelta

import pytest
from sqlalchemy import select

from src.backend_ai.gateway.files.service import FileLimits, GatewayFileService
from src.backend_ai.workers.file_maintenance import maintain_files
from src.shared.models.core import utc_now
from src.shared.models.files import FileDeletionOutbox, FileQuotaReservation
from tests.test_gateway.test_files import files_env as files_env


@pytest.mark.asyncio
async def test_delete_has_durable_lifecycle_then_worker_retries(files_env, monkeypatch):
    session, storage, app_id, _ = files_env
    service = GatewayFileService(session, storage)
    record = await service.create(app_id, filename="a.txt", data=b"ok", declared_mime_type="text/plain")
    original_delete = storage.delete

    async def unavailable(key):
        raise ConnectionError("private-storage-url")

    monkeypatch.setattr(storage, "delete", unavailable)
    deleted = await service.delete(app_id, record.id)
    assert deleted.lifecycle == "pending_delete"
    assert await session.get(FileDeletionOutbox, record.id) is not None
    monkeypatch.setattr(storage, "delete", original_delete)
    assert await service.drain_deletions() == 1
    await session.refresh(record)
    assert record.lifecycle == "deleted"
    assert await service.drain_deletions() == 0


@pytest.mark.asyncio
async def test_expired_reservation_is_recovered_only_by_its_storage_backend(files_env):
    session, storage, app_id, _ = files_env
    session.add(FileQuotaReservation(id="file-pending", application_id=app_id, bytes=2,
        storage_backend="s3", expires_at=utc_now() - timedelta(minutes=1)))
    await session.commit()
    result = await maintain_files(session, storage, FileLimits())
    assert result["recovered"] == 0
    assert await session.scalar(select(FileQuotaReservation.id)) == "file-pending"


@pytest.mark.asyncio
async def test_two_workers_claim_each_outbox_entry_once(files_env, monkeypatch):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker
    session, storage, app_id, _ = files_env
    service = GatewayFileService(session, storage)
    record = await service.create(app_id, filename='a.txt', data=b'ok', declared_mime_type='text/plain')
    async def outage(key):
        raise ConnectionError('unavailable')
    monkeypatch.setattr(storage, 'delete', outage)
    await service.delete(app_id, record.id)
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()
    original_key = record.storage_key
    async def blocked(key):
        calls.append(key)
        entered.set()
        await release.wait()
    monkeypatch.setattr(storage, 'delete', blocked)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    async with factory() as first, factory() as second:
        worker = asyncio.create_task(GatewayFileService(first, storage).drain_deletions())
        await asyncio.wait_for(entered.wait(), 1)
        other = asyncio.create_task(GatewayFileService(second, storage).drain_deletions())
        await asyncio.sleep(0.05)
        assert calls == [original_key], 'A durable lease must fence the other worker during storage I/O'
        release.set()
        assert await worker == 1
        assert await other == 0


@pytest.mark.asyncio
async def test_expired_upload_owner_cannot_commit_metadata(files_env):
    from sqlalchemy import update

    from src.backend_ai.services.file_quota_service import FileQuotaService
    from src.shared.models.files import GatewayFileRecord
    session, storage, app_id, _ = files_env
    quota = FileQuotaService(session, 100)
    await quota.reserve(app_id, 2, 'file-expired-writer')
    await session.execute(update(FileQuotaReservation).where(FileQuotaReservation.id == 'file-expired-writer')
        .values(expires_at=utc_now() - timedelta(seconds=1)))
    await session.commit()
    record = GatewayFileRecord(id='file-expired-writer', application_id=app_id, filename='a.txt',
        purpose='user_data', mime_type='text/plain', bytes=2, sha256='0'*64,
        storage_backend='local', storage_key=app_id+'/file-expired-writer', expires_at=utc_now()+timedelta(days=1))
    session.add(record)
    with pytest.raises(RuntimeError, match='lease'):
        await quota.commit('file-expired-writer', 'file-expired-writer')
    await session.rollback()


@pytest.mark.asyncio
async def test_file_reads_use_record_backend_after_default_backend_changes(files_env):
    from src.backend_ai.gateway.files.storage import S3FileStorage
    from tests.test_gateway.test_files import FakeS3
    session, local, app_id, _ = files_env
    s3 = S3FileStorage('fixture', client=FakeS3())
    record = await GatewayFileService(session, local).create(app_id, filename='a.txt', data=b'ok', declared_mime_type='text/plain')
    service = GatewayFileService(session, s3, storage_resolver=lambda backend: {'local': local, 's3': s3}[backend])
    _, content = await service.content(app_id, record.id)
    assert content == b'ok'


@pytest.mark.asyncio
async def test_maintenance_recovers_old_backend_after_default_switch(files_env, monkeypatch):
    from types import SimpleNamespace

    import src.backend_ai.workers.file_maintenance as maintenance
    session, local, app_id, _ = files_env
    await local.put(app_id+'/file-old-writer', b'ok', content_type='text/plain')
    session.add(FileQuotaReservation(id='file-old-writer', application_id=app_id, bytes=2,
        storage_backend='local', expires_at=utc_now()-timedelta(minutes=1)))
    await session.commit()
    fake_s3 = SimpleNamespace(name='s3')
    monkeypatch.setattr(maintenance, 'get_file_storage', lambda settings, backend=None:
        {'local':local, 's3':fake_s3}[backend or 's3'])
    monkeypatch.setattr(maintenance, 'build_file_limits', lambda settings: FileLimits())
    result = await maintenance.maintain_configured_files(session, SimpleNamespace(gateway_files_storage='s3'))
    assert result['recovered'] == 1
    assert await session.get(FileQuotaReservation, 'file-old-writer') is None
    assert not (local.root/app_id/'file-old-writer').exists()


@pytest.mark.asyncio
async def test_lost_commit_acknowledgement_keeps_committed_file(files_env, monkeypatch):
    from src.backend_ai.services.file_quota_service import FileQuotaService
    session, storage, app_id, _ = files_env
    original = FileQuotaService.commit
    async def lost_ack(self,*args,**kwargs):
        await original(self,*args,**kwargs)
        raise ConnectionError('Injected lost commit acknowledgement')
    monkeypatch.setattr(FileQuotaService,'commit',lost_ack)
    record = await GatewayFileService(session,storage).create(app_id,filename='a.txt',data=b'ok',declared_mime_type='text/plain')
    assert record.lifecycle == 'active' and await storage.get(record.storage_key) == b'ok'
    assert await session.get(FileQuotaReservation,record.id) is None
