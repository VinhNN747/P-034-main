"""Create only support chat tables on the older SDK migration branch.

The live database may be at revision 0008_gateway_runtime_sdk, which is not in
this checkout's Alembic graph. This repair leaves alembic_version untouched.
Run without arguments for a read-only preflight; --apply performs the DDL.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, inspect, text

from src.shared.config import get_settings
from src.shared.models.support_chat import SupportChatMessageRecord, SupportChatSessionRecord

EXPECTED_REVISION = "0008_gateway_runtime_sdk"
REQUIRED_TABLES = ("applications", "gateway_requests", "usage_events")
CHAT_TABLES = ("support_chat_sessions", "support_chat_messages")


def sync_database_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql+psycopg://"):
        return value
    raise RuntimeError("This repair supports only the configured PostgreSQL database")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create support chat tables in the configured database")
    parser.add_argument("--verify", action="store_true", help="Verify the created tables without writing")
    args = parser.parse_args()
    if args.apply and args.verify:
        parser.error("--apply and --verify cannot be combined")
    engine = create_engine(
        sync_database_url(get_settings().database_url),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    applied = False
    try:
        with engine.begin() as connection:
            if not args.apply:
                connection.execute(text("SET TRANSACTION READ ONLY"))
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            if revision != [EXPECTED_REVISION]:
                raise RuntimeError(f"Unexpected Alembic revision: {revision!r}; no schema change made")

            inspector = inspect(connection)
            missing_domain = [name for name in REQUIRED_TABLES if not inspector.has_table(name, schema="public")]
            if missing_domain:
                raise RuntimeError(f"Expected domain tables are missing: {missing_domain!r}; no schema change made")

            present = [inspector.has_table(name, schema="public") for name in CHAT_TABLES]
            if args.verify:
                if not all(present):
                    raise RuntimeError(f"Support chat tables are missing: {present!r}")
                records = connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, "
                        "has_table_privilege('anon', oid, 'SELECT'), "
                        "has_table_privilege('authenticated', oid, 'SELECT') "
                        "FROM pg_class WHERE oid IN (to_regclass(:sessions), to_regclass(:messages))"
                    ),
                    {"sessions": "public.support_chat_sessions", "messages": "public.support_chat_messages"},
                ).all()
                if len(records) != 2 or any(not row[1] or row[2] or row[3] for row in records):
                    raise RuntimeError("Support chat table RLS or Data API privileges do not match the migration")
                print("Verification OK: both support chat tables exist, RLS is enabled, Data API reads are revoked.")
                return
            if any(present):
                raise RuntimeError(
                    f"Support chat tables already exist (sessions={present[0]}, messages={present[1]}); "
                    "inspect the schema before changing it"
                )

            if not args.apply:
                print(f"Preflight OK: revision {EXPECTED_REVISION}; chat tables absent. Use --apply to create them.")
                return

            connection.execute(text("SET LOCAL search_path TO public"))
            SupportChatSessionRecord.__table__.create(bind=connection, checkfirst=False)
            SupportChatMessageRecord.__table__.create(bind=connection, checkfirst=False)
            for table_name in CHAT_TABLES:
                connection.execute(text(f'ALTER TABLE public."{table_name}" ENABLE ROW LEVEL SECURITY'))
                connection.execute(
                    text(f'REVOKE ALL PRIVILEGES ON TABLE public."{table_name}" FROM anon, authenticated')
                )
            applied = True
    finally:
        engine.dispose()
    if applied:
        print("Created support chat tables with RLS and revoked Data API access.")
        print("Alembic revision was left unchanged; reconcile the SDK migration branch before future upgrades.")


if __name__ == "__main__":
    main()
