from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
LATENCY_TABLES = {"latency_observations", "latency_training_runs", "latency_model_state"}


def _run_alembic(raw_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "DATABASE_URL": raw_url, "PGCLIENTENCODING": "UTF8"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "src/alembic.ini", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(arguments)} failed: {result.stderr[-4000:]}"
    return result


def _pg_client_environment(url) -> dict[str, str]:
    environment = {**os.environ, "PGDATABASE": url.database or "", "PGCLIENTENCODING": "UTF8"}
    if url.host:
        environment["PGHOST"] = url.host
    if url.port:
        environment["PGPORT"] = str(url.port)
    if url.username:
        environment["PGUSER"] = url.username
    if url.password is not None:
        environment["PGPASSWORD"] = url.password
    for parameter in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
        value = url.query.get(parameter)
        if value is not None:
            environment["PG" + parameter.upper()] = str(value)
    return environment


def test_postgresql_migration_backup_upgrade_downgrade_upgrade(tmp_path):
    """Exercise the real Alembic chain only in an explicitly empty disposable DB."""
    raw_url = os.getenv("LATENCY_TEST_MIGRATION_POSTGRES_URL")
    if not raw_url:
        pytest.skip("set LATENCY_TEST_MIGRATION_POSTGRES_URL to a dedicated disposable database")
    if not re.search(r"(test|disposable|latency_migration)", make_url(raw_url).database or "", re.I):
        pytest.skip("migration URL must name an explicitly disposable test database")
    pg_dump_binary = shutil_which("pg_dump")
    if not pg_dump_binary:
        pytest.skip("pg_dump and pg_restore are required for the migration backup check")
    pg_restore_binary = str(Path(pg_dump_binary).with_name("pg_restore"))
    if not Path(pg_restore_binary).is_file():
        pytest.skip("matching pg_restore binary was not found beside pg_dump")

    url = make_url(raw_url)
    psycopg_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    pg_environment = _pg_client_environment(url)
    with psycopg.connect(psycopg_url, autocommit=True) as connection:
        existing = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
        if existing:
            pytest.skip("migration test requires an empty disposable database; refusing to alter existing tables")

    backup = tmp_path / f"latency-migration-before-{uuid4().hex}.dump"
    dump = subprocess.run(
        [pg_dump_binary, "--format=custom", "--file", str(backup)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=pg_environment,
    )
    assert dump.returncode == 0 and backup.is_file() and backup.stat().st_size > 0
    toc = subprocess.run(
        [pg_restore_binary, "--list", str(backup)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=pg_environment,
    )
    assert toc.returncode == 0 and "Archive created at" in toc.stdout

    # These Supabase-owned roles/tables are external prerequisites assumed by
    # existing repository migrations, so model them only in this disposable DB.
    with psycopg.connect(psycopg_url, autocommit=True) as connection:
        for role in ("anon", "authenticated"):
            connection.execute(
                f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN; "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        connection.execute(
            "CREATE TABLE public.permissions ("
            "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), code text UNIQUE NOT NULL, "
            "resource text NOT NULL, action text NOT NULL, description text NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE public.roles (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), code text UNIQUE NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE public.role_permissions (role_id uuid NOT NULL, permission_id uuid NOT NULL, "
            "PRIMARY KEY (role_id, permission_id))"
        )

    _run_alembic(raw_url, "upgrade", "head")
    with psycopg.connect(psycopg_url) as connection:
        current = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        assert len(current) == 1
        assert current[0][0] == "20261014_adaptive_latency"
        tables = {
            row[0] for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
        }
        assert LATENCY_TABLES <= tables
        assert {"models", "applications", "billing_transactions", "gateway_requests"} <= tables
        assert connection.execute(
            "SELECT count(*) FROM latency_model_state WHERE scope='generation-v1'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace "
            "AND relname IN ('latency_observations','latency_training_runs','latency_model_state') "
            "AND relrowsecurity"
        ).fetchone()[0] == 3
        indices = {
            row[0] for row in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
            ).fetchall()
        }
        assert {
            "ix_latency_observations_group_finished",
            "ix_latency_observations_finished",
            "ix_latency_observations_request_id",
            "ix_latency_training_runs_created",
            "ix_latency_model_state_lease_expiry",
        } <= indices

    _run_alembic(raw_url, "downgrade", "-1")
    with psycopg.connect(psycopg_url) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
        }
        assert not LATENCY_TABLES.intersection(tables)
        assert {"models", "applications", "billing_transactions", "gateway_requests"} <= tables

    _run_alembic(raw_url, "upgrade", "head")
    with psycopg.connect(psycopg_url) as connection:
        assert connection.execute("SELECT count(*) FROM latency_model_state").fetchone()[0] == 1
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("20261014_adaptive_latency",)
        ]
    print(f"LATENCY_MIGRATION_BACKUP {backup} bytes={backup.stat().st_size} pg_restore_list=valid")


def shutil_which(binary: str) -> str | None:
    from shutil import which

    return which(binary)

