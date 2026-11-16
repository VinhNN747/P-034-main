from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.runtime import (
    AggregationResultRecord,
    GatewayRequestRecord,
    GatewayTaskRecord,
    ModelExecutionRecord,
    RouteDecisionRecord,
    StageTraceRecord,
)


class GatewayPersistenceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def request(self, request_id: str) -> GatewayRequestRecord | None:
        return await self.session.get(GatewayRequestRecord, request_id)

    async def tasks(self, request_id: str) -> list[GatewayTaskRecord]:
        result = await self.session.scalars(
            select(GatewayTaskRecord).where(GatewayTaskRecord.request_id == request_id).order_by(GatewayTaskRecord.id)
        )
        return list(result)

    async def route_decisions(self, request_id: str) -> list[RouteDecisionRecord]:
        result = await self.session.scalars(
            select(RouteDecisionRecord)
            .where(RouteDecisionRecord.request_id == request_id)
            .order_by(RouteDecisionRecord.id)
        )
        return list(result)

    async def executions(self, request_id: str) -> list[ModelExecutionRecord]:
        result = await self.session.scalars(
            select(ModelExecutionRecord)
            .where(ModelExecutionRecord.request_id == request_id)
            .order_by(ModelExecutionRecord.id)
        )
        return list(result)

    async def aggregation(self, request_id: str) -> AggregationResultRecord | None:
        return await self.session.scalar(
            select(AggregationResultRecord).where(AggregationResultRecord.request_id == request_id)
        )

    async def traces(self, request_id: str) -> list[StageTraceRecord]:
        result = await self.session.scalars(
            select(StageTraceRecord)
            .where(StageTraceRecord.request_id == request_id)
            .order_by(StageTraceRecord.started_at, StageTraceRecord.id)
        )
        return list(result)
