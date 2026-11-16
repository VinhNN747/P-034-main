"""Durable reservations; storage I/O never holds the application DB lock."""

from datetime import timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.core import Application, utc_now
from src.shared.models.files import FileQuotaReservation, GatewayFileRecord


class FileQuotaExceededError(Exception):
    pass


class FileQuotaService:
    def __init__(self, session: AsyncSession, quota_bytes: int):
        self.session, self.quota_bytes = session, quota_bytes

    async def reserve(self, application_id: str, size: int, reservation_id: str, *, storage_backend="local") -> str:
        if size <= 0:
            raise ValueError("Reservation size must be positive")
        if self.session.in_transaction():
            # Caller has only auth/read work before reserve. Refuse to discard writes.
            if self.session.new or self.session.dirty or self.session.deleted:
                raise RuntimeError("Reservation requires a clean session")
            await self.session.rollback()
        try:
            if self.session.get_bind().dialect.name == "sqlite":
                await self.session.execute(text("BEGIN IMMEDIATE"))
            else:
                await self.session.begin()
                await self.session.scalar(
                    select(Application.id).where(Application.id == application_id).with_for_update()
                )
            now = utc_now()
            active = await self.session.scalar(
                select(func.coalesce(func.sum(GatewayFileRecord.bytes), 0)).where(
                    GatewayFileRecord.application_id == application_id,
                    GatewayFileRecord.deleted_at.is_(None),
                    GatewayFileRecord.expires_at > now,
                )
            )
            # Expired unfinished reservations remain charged until maintenance
            # deletes their possible blobs. Expiry alone must not free quota.
            reserved = await self.session.scalar(
                select(func.coalesce(func.sum(FileQuotaReservation.bytes), 0)).where(
                    FileQuotaReservation.application_id == application_id, FileQuotaReservation.file_id.is_(None)
                )
            )
            if int(active or 0) + int(reserved or 0) + size > self.quota_bytes:
                raise FileQuotaExceededError("Application storage quota would be exceeded.")
            self.session.add(
                FileQuotaReservation(
                    id=reservation_id, application_id=application_id, bytes=size,
                    storage_backend=storage_backend, expires_at=now + timedelta(minutes=10)
                )
            )
            await self.session.commit()
            return reservation_id
        except BaseException:
            await self.session.rollback()
            raise

    async def commit(self, reservation_id: str, file_id: str) -> None:
        # The same conditional lock fences a recovery worker racing metadata
        # commit. An expired writer cannot publish a blob already reclaimed.
        locked = await self.session.execute(update(FileQuotaReservation).where(
            FileQuotaReservation.id == reservation_id, FileQuotaReservation.state == "writing",
            FileQuotaReservation.expires_at > utc_now()).values(state="committing"))
        if locked.rowcount != 1:
            raise RuntimeError("File upload lease no longer belongs to this writer")
        reservation = await self.session.get(FileQuotaReservation, reservation_id, populate_existing=True)
        if reservation is None:
            raise RuntimeError("File quota reservation no longer exists")
        record = await self.session.get(GatewayFileRecord, file_id)
        if record is None or record.application_id != reservation.application_id or record.bytes != reservation.bytes:
            raise RuntimeError("File does not match its quota reservation")
        await self.session.delete(reservation)
        # File metadata and reservation removal commit in the same transaction.
        await self.session.commit()

    async def renew(self, reservation_id: str) -> bool:
        result = await self.session.execute(update(FileQuotaReservation).where(
            FileQuotaReservation.id == reservation_id, FileQuotaReservation.state == "writing",
            FileQuotaReservation.expires_at > utc_now()).values(expires_at=utc_now() + timedelta(minutes=10)))
        await self.session.commit()
        return result.rowcount == 1

    async def release(self, reservation_id: str) -> None:
        await self.session.rollback()
        await self.session.execute(delete(FileQuotaReservation).where(FileQuotaReservation.id == reservation_id))
        await self.session.commit()
