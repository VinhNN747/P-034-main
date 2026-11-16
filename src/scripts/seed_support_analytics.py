"""Seed realistic gateway usage so the support chatbot can render charts.

The seed is additive and idempotent for each application/date/slot. It never
updates or deletes real gateway records.
"""

# The CLI supports `python src/scripts/seed_support_analytics.py` from the repository root.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.shared.database.session import create_engine_from_settings
from src.shared.models.core import Application
from src.shared.models.runtime import GatewayRequestRecord, UsageEventRecord

SEED_NAMESPACE = UUID("9fb3b789-a6cf-4ac4-934f-901be55fa9e1")
SEED_TIMEZONE = ZoneInfo("Asia/Bangkok")
MODELS = (
    ("openai", "gpt-4o-mini", 0.000_000_45),
    ("google", "gemini-3.6-flash", 0.000_001_10),
    ("openai", "gpt-5.6-terra", 0.000_003_80),
)
SOURCES = ("playground", "api", "sdk")


@dataclass(frozen=True)
class AnalyticsSeedSummary:
    applications: int
    requested_rows: int
    requests_inserted: int
    usage_events_inserted: int


def _request_id(application_id: str, day: date, slot: int) -> str:
    value = uuid5(SEED_NAMESPACE, f"request:{application_id}:{day.isoformat()}:{slot}")
    return f"chart-seed-{value}"


def _usage_id(application_id: str, day: date, slot: int) -> str:
    return str(uuid5(SEED_NAMESPACE, f"usage:{application_id}:{day.isoformat()}:{slot}"))


def _occurred_at(day: date, slot: int) -> datetime:
    # All generated timestamps are safely in the past because the default end
    # date is yesterday. Spacing slots also makes request-level charts readable.
    local = datetime.combine(day, time(hour=1, minute=(slot * 7) % 60), SEED_TIMEZONE)
    return (local + timedelta(hours=slot // 8)).astimezone(UTC)


async def _applications(
    session: AsyncSession, *, owner_id: str | None, application_ids: list[str] | None
) -> list[Application]:
    statement = select(Application).order_by(Application.created_at, Application.id)
    if application_ids:
        statement = statement.where(Application.id.in_(application_ids))
    elif owner_id:
        statement = statement.where(Application.owner_user_id == owner_id)
    else:
        raise ValueError("Provide --owner-id or at least one --application-id.")

    applications = list(await session.scalars(statement))
    if application_ids:
        found = {application.id for application in applications}
        missing = sorted(set(application_ids) - found)
        if missing:
            raise ValueError(f"Application not found: {', '.join(missing)}")
    if owner_id:
        foreign = [app.id for app in applications if app.owner_user_id != owner_id]
        if foreign:
            raise ValueError(f"Applications do not belong to owner {owner_id}: {', '.join(foreign)}")
    if not applications:
        target = f"owner {owner_id}" if owner_id else "the selected IDs"
        raise ValueError(f"No applications found for {target}.")
    return applications


async def seed_session(
    session: AsyncSession,
    *,
    owner_id: str | None = None,
    application_ids: list[str] | None = None,
    days: int = 70,
    requests_per_day: int = 3,
    end_date: date | None = None,
    dry_run: bool = False,
) -> AnalyticsSeedSummary:
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    if not 1 <= requests_per_day <= 12:
        raise ValueError("requests_per_day must be between 1 and 12")

    applications = await _applications(
        session, owner_id=owner_id, application_ids=application_ids
    )
    final_day = end_date or (datetime.now(SEED_TIMEZONE).date() - timedelta(days=1))
    first_day = final_day - timedelta(days=days - 1)

    specs: list[tuple[Application, int, date, int]] = []
    for app_index, application in enumerate(applications):
        for day_offset in range(days):
            day = first_day + timedelta(days=day_offset)
            for slot in range(requests_per_day):
                specs.append((application, app_index, day, slot))

    request_ids = [_request_id(app.id, day, slot) for app, _, day, slot in specs]
    usage_ids = [_usage_id(app.id, day, slot) for app, _, day, slot in specs]
    existing_requests = set(
        await session.scalars(
            select(GatewayRequestRecord.request_id).where(
                GatewayRequestRecord.request_id.in_(request_ids)
            )
        )
    )
    existing_usage = set(
        await session.scalars(
            select(UsageEventRecord.id).where(UsageEventRecord.id.in_(usage_ids))
        )
    )

    pending_requests: list[GatewayRequestRecord] = []
    pending_usage: list[UsageEventRecord] = []
    for index, (application, app_index, day, slot) in enumerate(specs):
        request_id = request_ids[index]
        usage_id = usage_ids[index]
        day_index = (day - first_day).days
        provider, model, price_factor = MODELS[(day_index + slot + app_index) % len(MODELS)]
        source = SOURCES[(day_index * 2 + slot + app_index) % len(SOURCES)]
        failed = (day_index * requests_per_day + slot + app_index * 3) % 17 == 0
        status = "failed" if failed else "completed"
        latency_ms = float(420 + app_index * 85 + slot * 130 + (day_index % 9) * 37)
        input_tokens = 520 + app_index * 90 + slot * 170 + day_index * 8
        output_tokens = 110 + app_index * 25 + slot * 55 + (day_index % 7) * 9
        cost_usd = round((input_tokens + output_tokens * 2.4) * price_factor, 6)
        occurred_at = _occurred_at(day, slot)

        if request_id not in existing_requests:
            pending_requests.append(
                GatewayRequestRecord(
                    request_id=request_id,
                    application_id=application.id,
                    prompt="Synthetic analytics seed request",
                    messages_json=[],
                    constraints_json={},
                    strategies_json={"seed": "support-analytics-v1"},
                    source=source,
                    response_json={} if failed else {"seeded": True},
                    status=status,
                    aggregation_status=status,
                    total_cost=cost_usd,
                    total_latency_ms=latency_ms,
                    started_at=occurred_at,
                    finished_at=occurred_at + timedelta(milliseconds=latency_ms),
                )
            )
        if usage_id not in existing_usage:
            pending_usage.append(
                UsageEventRecord(
                    id=usage_id,
                    request_id=request_id,
                    application_id=application.id,
                    provider=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    estimated=True,
                    source=source,
                    created_at=occurred_at,
                )
            )

    if not dry_run:
        session.add_all(pending_requests)
        await session.flush()
        session.add_all(pending_usage)
        await session.commit()

    return AnalyticsSeedSummary(
        applications=len(applications),
        requested_rows=len(specs),
        requests_inserted=len(pending_requests),
        usage_events_inserted=len(pending_usage),
    )


async def seed_database(
    database_url: str | None = None,
    **kwargs,
) -> AnalyticsSeedSummary:
    engine = create_engine_from_settings(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            return await seed_session(session, **kwargs)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--owner-id")
    parser.add_argument("--application-id", action="append", dest="application_ids")
    parser.add_argument("--days", type=int, default=70)
    parser.add_argument("--requests-per-day", type=int, default=3)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        summary = asyncio.run(
            seed_database(
                args.database_url,
                owner_id=args.owner_id,
                application_ids=args.application_ids,
                days=args.days,
                requests_per_day=args.requests_per_day,
                end_date=args.end_date,
                dry_run=args.dry_run,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(summary)


if __name__ == "__main__":
    main()
