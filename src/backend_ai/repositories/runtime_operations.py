from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.runtime import RuntimeOperationRecord


class RuntimeOperationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def by_idempotency_key(self, application_id: str, key_hash: str) -> RuntimeOperationRecord | None:
        return await self.session.scalar(
            select(RuntimeOperationRecord).where(
                RuntimeOperationRecord.application_id == application_id,
                RuntimeOperationRecord.idempotency_key_hash == key_hash,
            )
        )

    async def reconcile_expired(self, application_id: str, now: datetime) -> int:
        result = await self.session.execute(
            update(RuntimeOperationRecord)
            .where(
                RuntimeOperationRecord.application_id == application_id,
                RuntimeOperationRecord.status == "running",
                RuntimeOperationRecord.expires_at <= now,
            )
            .values(status="indeterminate", finished_at=now)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    async def count_since(self, application_id: str, since: datetime) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(RuntimeOperationRecord)
                .where(
                    RuntimeOperationRecord.application_id == application_id,
                    RuntimeOperationRecord.started_at >= since,
                )
            )
            or 0
        )

    async def count_running(self, application_id: str, now: datetime) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(RuntimeOperationRecord)
                .where(
                    RuntimeOperationRecord.application_id == application_id,
                    RuntimeOperationRecord.status == "running",
                    RuntimeOperationRecord.expires_at > now,
                )
            )
            or 0
        )

    async def add(self, operation: RuntimeOperationRecord) -> None:
        self.session.add(operation)
        await self.session.flush()

    async def scoped(self, application_id: str, request_id: str) -> RuntimeOperationRecord | None:
        return await self.session.scalar(
            select(RuntimeOperationRecord).where(
                RuntimeOperationRecord.application_id == application_id,
                RuntimeOperationRecord.request_id == request_id,
            )
        )

    async def finalize(
        self,
        request_id: str,
        *,
        status: str,
        public_result: dict,
        http_status: int,
        http_body: dict,
        finished_at: datetime,
    ) -> bool:
        result = await self.session.execute(
            update(RuntimeOperationRecord)
            .where(RuntimeOperationRecord.request_id == request_id, RuntimeOperationRecord.status == "running")
            .values(
                status=status,
                public_result_json=public_result,
                http_status=http_status,
                http_body_json=http_body,
                finished_at=finished_at,
            )
        )
        return (result.rowcount or 0) == 1
