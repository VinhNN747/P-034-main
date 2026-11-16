from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.audit import AuditEventRecord


class AuditRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_event(
        self,
        *,
        actor_user_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str,
        request_id: str | None = None,
        metadata: dict | None = None,
    ) -> AuditEventRecord:
        event = AuditEventRecord(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
            metadata_json=metadata or {},
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def for_resource(self, resource_type: str, resource_id: str) -> list[AuditEventRecord]:
        result = await self.session.scalars(
            select(AuditEventRecord)
            .where(
                AuditEventRecord.resource_type == resource_type,
                AuditEventRecord.resource_id == resource_id,
            )
            .order_by(AuditEventRecord.created_at, AuditEventRecord.id)
        )
        return list(result)
