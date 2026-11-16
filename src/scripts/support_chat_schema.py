"""Inspect or apply guarded support migrations; reconciliation defaults to a dry run."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from alembic import command  # noqa: E402
from src.shared.config import get_settings  # noqa: E402
from src.shared.models.support_chat import SupportChatRateLimitRecord  # noqa: E402

PARENT = "0011_gateway_direct_comparisons"
TARGET = "0012_support_chat_context"


def schema_engine():
    url = make_url(get_settings().database_url)
    if url.get_backend_name() == "postgresql" or url.drivername == "postgres":
        url = url.set(drivername="postgresql+psycopg")
    elif url.get_backend_name() == "sqlite":
        url = url.set(drivername="sqlite")
    else:
        raise RuntimeError("Unsupported database")
    return create_engine(url, connect_args={"connect_timeout": 5} if url.get_backend_name() == "postgresql" else {})


def inspect_schema() -> dict:
    engine = schema_engine()
    try:
        with engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
            inspector = inspect(connection)
            revisions = list(connection.scalars(text("SELECT version_num FROM alembic_version")))
            sessions = inspector.has_table("support_chat_sessions")
            messages = inspector.has_table("support_chat_messages")
            return {
                "revisions": revisions,
                "sessions": sessions,
                "messages": messages,
                "state_column": sessions
                and "state_json" in {c["name"] for c in inspector.get_columns("support_chat_sessions")},
                "scope_column": messages
                and "application_id" in {c["name"] for c in inspector.get_columns("support_chat_messages")},
                "rate_limit_table": inspector.has_table("support_chat_rate_limits"),
            }
    finally:
        engine.dispose()


def isolated_upgrade(expected_revision: str) -> None:
    """Add only support columns/table in one transaction, preserving external history."""
    engine = schema_engine()
    try:
        with engine.begin() as connection:
            revisions = list(
                connection.scalars(
                    text(
                        "SELECT version_num FROM alembic_version"
                        + (" FOR UPDATE" if connection.dialect.name == "postgresql" else "")
                    )
                )
            )
            if revisions != [expected_revision]:
                raise RuntimeError("External revision changed; no schema changes made")
            inspector = inspect(connection)
            required = {
                "applications": {"id", "owner_user_id"},
                "support_chat_sessions": {"id", "user_id", "title", "created_at", "updated_at"},
                "support_chat_messages": {
                    "id",
                    "session_id",
                    "user_id",
                    "role",
                    "content",
                    "status",
                    "required_permission",
                    "sources_json",
                    "actions_json",
                    "created_at",
                },
            }
            columns = {}
            for name, expected_columns in required.items():
                if not inspector.has_table(name):
                    raise RuntimeError("Missing expected support/domain table")
                columns[name] = {column["name"] for column in inspector.get_columns(name)}
                if not expected_columns <= columns[name]:
                    raise RuntimeError("Existing table does not match support contract")
            if (
                "state_json" in columns["support_chat_sessions"]
                or "application_id" in columns["support_chat_messages"]
                or inspector.has_table("support_chat_rate_limits")
            ):
                raise RuntimeError("Partial or previously applied upgrade; inspect before changing")
            connection.execute(
                text("ALTER TABLE support_chat_sessions ADD COLUMN state_json JSON NOT NULL DEFAULT '{}'")
            )
            connection.execute(text("ALTER TABLE support_chat_messages ADD COLUMN application_id VARCHAR(100)"))
            SupportChatRateLimitRecord.__table__.create(bind=connection)
            if connection.dialect.name == "postgresql":
                connection.execute(text("ALTER TABLE public.support_chat_rate_limits ENABLE ROW LEVEL SECURITY"))
                connection.execute(
                    text("REVOKE ALL PRIVILEGES ON TABLE public.support_chat_rate_limits FROM anon, authenticated")
                )
    finally:
        engine.dispose()


def context_contract(connection) -> list[str]:
    """Verify every schema/security effect before recording an already-applied revision."""
    from sqlalchemy import JSON, DateTime, Integer, String

    inspector = inspect(connection)
    issues = []
    for table, expected in {
        "support_chat_sessions": {"state_json": (JSON, False)},
        "support_chat_messages": {"application_id": (String, True)},
        "support_chat_rate_limits": {
            "user_id": (String, False),
            "window_started_at": (DateTime, False),
            "request_count": (Integer, False),
        },
    }.items():
        if not inspector.has_table(table):
            issues.append(f"Missing table: {table}")
            continue
        columns = {item["name"]: item for item in inspector.get_columns(table)}
        for name, (kind, nullable) in expected.items():
            column = columns.get(name)
            if column is None or not isinstance(column["type"], kind) or column["nullable"] != nullable:
                issues.append(f"Column contract mismatch: {table}.{name}")
        if table == "support_chat_rate_limits" and inspector.get_pk_constraint(table)["constrained_columns"] != [
            "user_id"
        ]:
            issues.append("Rate-limit primary key mismatch")
    if connection.dialect.name == "postgresql":
        row = connection.execute(
            text(
                "SELECT c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relname='support_chat_rate_limits'"
            )
        ).first()
        if not row or not row[0]:
            issues.append("Rate-limit table RLS is not enabled")
        if row:
            for role in ("anon", "authenticated"):
                if connection.scalar(
                    text(
                        "SELECT has_table_privilege(:role, 'public.support_chat_rate_limits', "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ),
                    {"role": role},
                ):
                    issues.append(f"Rate-limit Data API privileges remain for {role}")
    return issues


def reconcile_context(*, apply: bool = False) -> dict:
    """Preserve the verified 0013 branch and reconcile context before analytics, atomically."""
    analytics_target = "20261008_support_analytics"
    engine = schema_engine()
    try:
        with engine.begin() as connection:
            if not apply and connection.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
            revisions = sorted(
                connection.scalars(
                    text(
                        "SELECT version_num FROM alembic_version"
                        + (" FOR UPDATE" if apply and connection.dialect.name == "postgresql" else "")
                    )
                )
            )
            issues = context_contract(connection)
            inspector = inspect(connection)
            has_data = inspector.has_table("support_chat_messages") and "data_json" in {
                item["name"] for item in inspector.get_columns("support_chat_messages")
            }
            indexes = inspector.get_indexes("usage_events") if inspector.has_table("usage_events") else []
            has_index = any(
                item["name"] == "ix_usage_events_app_created"
                and item["column_names"] == ["application_id", "created_at"]
                for item in indexes
            )
            if revisions == [analytics_target] and not issues and has_data and has_index:
                return {"revisions": revisions, "ready": True, "already_current": True}
            if revisions != ["0013_drop_custom_pool"]:
                issues.append("Expected the verified 0013_drop_custom_pool branch; no changes allowed")
            if has_data or any(item["name"] == "ix_usage_events_app_created" for item in indexes):
                issues.append("Analytics partially/already applied outside history; inspect separately")
            if not inspector.has_table("usage_events"):
                issues.append("Missing usage_events table")
            elif not {"application_id", "created_at"} <= {
                item["name"] for item in inspector.get_columns("usage_events")
            }:
                issues.append("Missing usage_events application/time columns")
            report = {
                "revisions": revisions,
                "ready": not issues,
                "issues": issues,
                "planned": [
                    "Record existing 0012_support_chat_context while preserving 0013_drop_custom_pool",
                    "Run empty branch merges",
                    "Add data_json and application/time index",
                ],
                "target": analytics_target,
            }
            if not apply:
                return report
            if issues:
                raise RuntimeError("Reconciliation blocked: " + "; ".join(issues))
            config = Config(str(PROJECT_ROOT / "src" / "alembic.ini"))
            config.attributes["connection"] = connection
            command.stamp(config, ["0013_drop_custom_pool", TARGET])
            command.upgrade(config, analytics_target)
            after = list(connection.scalars(text("SELECT version_num FROM alembic_version")))
            updated = inspect(connection)
            if after != [analytics_target] or "data_json" not in {
                item["name"] for item in updated.get_columns("support_chat_messages")
            }:
                raise RuntimeError("Reconciliation verification failed; transaction rolled back")
            if not any(
                item["name"] == "ix_usage_events_app_created"
                and item["column_names"] == ["application_id", "created_at"]
                for item in updated.get_indexes("usage_events")
            ):
                raise RuntimeError("Analytics index verification failed; transaction rolled back")
            return {"revisions": after, "ready": True, "applied": True}
    finally:
        engine.dispose()


def main(apply: bool, isolated_revision: str | None = None) -> None:
    state = inspect_schema()
    print(json.dumps(state))
    if not apply:
        return
    ready = state["state_column"] and state["scope_column"] and state["rate_limit_table"]
    if (state["revisions"] == [TARGET] or state["revisions"] == [isolated_revision]) and ready:
        print("Support schema already current")
        return
    if isolated_revision is not None:
        isolated_upgrade(isolated_revision)
        result = inspect_schema()
        if result["revisions"] != [isolated_revision] or not all(
            result[key] for key in ("state_column", "scope_column", "rate_limit_table")
        ):
            raise RuntimeError("Isolated support schema verification failed")
        print("Support schema upgraded and verified; external Alembic revision unchanged")
        return
    if (
        state["revisions"] != [PARENT]
        or not state["sessions"]
        or not state["messages"]
        or state["state_column"]
        or state["scope_column"]
        or state["rate_limit_table"]
    ):
        raise RuntimeError("Schema does not match the exact migration parent; no changes made")
    command.upgrade(Config(str(PROJECT_ROOT / "src" / "alembic.ini")), TARGET)
    result = inspect_schema()
    if result["revisions"] != [TARGET] or not all(
        result[key] for key in ("state_column", "scope_column", "rate_limit_table")
    ):
        raise RuntimeError("Support schema verification failed")
    print("Support schema upgraded and verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--isolated-revision", help="Guard an isolated support-only upgrade on an externally managed revision"
    )
    parser.add_argument(
        "--reconcile-context",
        action="store_true",
        help="Verify and reconcile already-applied context on the known 0013 branch; dry-run unless --apply",
    )
    args = parser.parse_args()
    try:
        if args.reconcile_context:
            if args.isolated_revision:
                raise RuntimeError("Cannot combine isolated and reconciliation modes")
            print(json.dumps(reconcile_context(apply=args.apply)))
        else:
            main(args.apply, args.isolated_revision)
    except Exception as error:
        print("Support schema operation failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
