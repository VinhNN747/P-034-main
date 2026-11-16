"""Recover expiry, deletion outbox, and interrupted upload reservations."""
import asyncio
import logging

from sqlalchemy import delete, func, select, update

from src.backend_ai.gateway.factory import build_file_limits, get_file_storage
from src.backend_ai.gateway.files.service import GatewayFileService
from src.shared.database.session import create_engine_from_settings, create_session_factory
from src.shared.models.core import utc_now
from src.shared.models.files import FileQuotaReservation

logger = logging.getLogger(__name__)


async def maintain_files(session, storage, limits, *, batch_size=100):
    service = GatewayFileService(session, storage, limits)
    expired = await service.purge_expired(limit=batch_size)
    deleted = await service.drain_deletions(limit=batch_size)
    now = utc_now()
    candidates = list(await session.scalars(select(FileQuotaReservation.id).where(
        FileQuotaReservation.expires_at <= now, FileQuotaReservation.file_id.is_(None),
        FileQuotaReservation.storage_backend == storage.name,
    ).limit(batch_size)))
    from datetime import timedelta
    if candidates:
        # Move expired writers into recovery atomically and extend the lease
        # so a second instance cannot delete the same uncommitted blob.
        rows = list((await session.execute(update(FileQuotaReservation).where(
            FileQuotaReservation.id.in_(candidates), FileQuotaReservation.expires_at <= now)
            .values(state="recovering", expires_at=now + timedelta(minutes=10))
            .returning(FileQuotaReservation.id, FileQuotaReservation.application_id))).all())
    else:
        rows = []
    await session.commit()
    recovered = 0
    for reservation_id, application_id in rows:
        try:
            await storage.delete(f'{application_id}/{reservation_id}')
        except Exception:
            continue  # quota remains charged until cleanup succeeds
        await session.execute(delete(FileQuotaReservation).where(
            FileQuotaReservation.id == reservation_id, FileQuotaReservation.state == "recovering"))
        await session.commit()
        recovered += 1
    return {'expired': expired, 'deleted': deleted, 'recovered': recovered}


async def run_file_maintenance(settings, stop_event):
    engine = create_engine_from_settings(settings=settings)
    factory = create_session_factory(engine)
    try:
        while not stop_event.is_set():
            try:
                async with factory() as session:
                    metrics = await maintain_configured_files(session, settings)
                    from src.backend_ai.services.openai_response_store import OpenAIResponseStore
                    metrics["responses_expired"] = await OpenAIResponseStore(session).expire()
                    from src.shared.models.files import FileDeletionOutbox
                    metrics["deletion_backlog"] = await session.scalar(select(func.count()).select_from(FileDeletionOutbox))
                    metrics["reservation_backlog"] = await session.scalar(select(func.count()).select_from(FileQuotaReservation))
                    logger.info("Gateway maintenance counters %s", metrics)
            except Exception:
                logger.warning('File maintenance failed; pending work remains durable')
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=300)
            except TimeoutError:
                pass
    finally:
        await engine.dispose()


async def maintain_configured_files(session, settings):
    """Recover stored backends independently of the current upload default."""
    from src.shared.models.files import FileDeletionOutbox, GatewayFileRecord
    backends = {settings.gateway_files_storage}
    for model in (GatewayFileRecord, FileQuotaReservation, FileDeletionOutbox):
        backends.update(await session.scalars(select(model.storage_backend).distinct()))
    metrics = {'expired':0, 'deleted':0, 'recovered':0, 'unavailable_backends':0}
    for backend in sorted(backends):
        try:
            result = await maintain_files(session, get_file_storage(settings, backend=backend), build_file_limits(settings))
        except Exception:
            await session.rollback()
            metrics['unavailable_backends'] += 1
            continue
        for field, value in result.items():
            metrics[field] += value
    return metrics
