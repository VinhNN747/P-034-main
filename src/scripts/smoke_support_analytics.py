"""Offline end-to-end support analytics smoke test; never reads the configured database."""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.backend.services.support_conversation_service import SupportConversationService
from src.shared.database.base import Base
from src.shared.dependencies.auth import CurrentUser
from src.shared.dtos.ai_internal import SupportUnderstandingOutput
from src.shared.models.core import Application
from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord


class OfflineUnderstanding:
    async def understand(self, **kwargs):
        return SupportUnderstandingOutput()


async def run(output: Path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            application = Application(id="demo-alpha", slug="demo-alpha", name="Alpha", owner_user_id="demo-user")
            session.add(application)
            await session.flush()
            now = datetime.now(UTC)
            for index in range(7):
                request_id = f"demo-{index}"
                created = now - timedelta(days=6 - index, minutes=5)
                session.add(
                    GatewayRequestRecord(
                        request_id=request_id,
                        application_id=application.id,
                        status="completed",
                        total_latency_ms=100 + index * 20,
                        started_at=created,
                    )
                )
                await session.flush()
                session.add(
                    UsageEventRecord(
                        request_id=request_id,
                        application_id=application.id,
                        model="demo-model",
                        input_tokens=100 * (index + 1),
                        output_tokens=50,
                        cost_usd=0.05 * (index + 1),
                        created_at=created,
                    )
                )
            await session.commit()
            actor = CurrentUser(
                id="demo-user",
                email="demo@example.com",
                roles=("user",),
                permissions=frozenset({"applications.read_own", "usage.read_own"}),
            )
            service = SupportConversationService(session, understanding=OfflineUnderstanding())
            response = await service.answer(
                question="Vẽ biểu đồ chi phí application Alpha theo ngày trong 7 ngày",
                current_user=actor,
                context=None,
                session_id=None,
            )
            assert response.data and abs(response.data.total - 1.4) < 1e-6
            data = await service.chart_data(actor, response.data.id)
            snapshot = data.model_dump_json(indent=2)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(snapshot, encoding="utf-8")
            print("PASS: SQL -> statistics -> private JSON snapshot for Highcharts")
            print(f"Preview: {output}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/support_analytics/chart-preview.json"))
    args = parser.parse_args()
    asyncio.run(run(args.output))
