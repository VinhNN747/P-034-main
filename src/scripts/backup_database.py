# Backs up the `public` schema of the database in .env (pg_dump 17 in Docker, custom format) and proves the
# backup restores: it loads the dump into a throwaway Postgres container and compares the row count of every table.
#
#   python src/scripts/backup_database.py --label before-<migration-name>
#
# Needs Docker running. Files go to ~/Documents/ai20k-db-backups (outside the repo: a dump holds encrypted provider
# credentials and customer data, so never commit it). Supabase's own `auth` schema is not included; use the
# Supabase dashboard backups for that. Restore errors about the `auth` schema and `supabase_*`/`authenticated`
# roles are expected: the throwaway Postgres has neither. The password reaches the containers through a temporary
# env file that is deleted afterwards; nothing secret is printed.
# ruff: noqa: E402
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from src.shared.config import get_settings

IMAGE = "postgres:17"  # pg_dump must be at least the server's major version (Supabase runs 17)


def table_counts(engine) -> dict[str, int]:
    with engine.connect() as connection:
        tables = [row[0] for row in connection.execute(text(
            "select table_name from information_schema.tables "
            "where table_schema='public' and table_type='BASE TABLE' order by 1"))]
        return {table: connection.execute(text(f'select count(*) from "{table}"')).scalar() for table in tables}


def dump(url, target_dir: Path, file_name: str) -> subprocess.CompletedProcess:
    fd, env_name = tempfile.mkstemp(suffix=".env")
    os.close(fd)  # Windows cannot delete a file that is still open
    env_file = Path(env_name)
    env_file.write_text(f"PGHOST={url.host}\nPGPORT={url.port}\nPGUSER={url.username}\nPGPASSWORD={url.password}\n"
                        f"PGDATABASE={url.database}\nPGSSLMODE=require\n", encoding="utf-8")
    try:
        return subprocess.run(
            ["docker", "run", "--rm", "--env-file", str(env_file), "-v", f"{target_dir}:/backup", IMAGE, "pg_dump",
             "--format=custom", "--schema=public", "--no-owner", "--no-privileges", "--file", f"/backup/{file_name}"],
            capture_output=True, text=True)
    finally:
        env_file.unlink(missing_ok=True)


def verify(target_dir: Path, file_name: str, expected: dict[str, int]) -> bool:
    name = "ai20k-restore-check"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=verify", "-v", f"{target_dir}:/backup:ro",
                    IMAGE], check=True, capture_output=True)
    try:
        for _ in range(60):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        time.sleep(2)
        subprocess.run(["docker", "exec", name, "createdb", "-U", "postgres", "verify"], check=True, capture_output=True)
        restore = subprocess.run(["docker", "exec", name, "pg_restore", "-U", "postgres", "-d", "verify", "--no-owner",
                                  "--no-privileges", f"/backup/{file_name}"], capture_output=True, text=True)
        errors = [line for line in restore.stderr.splitlines() if line.startswith("pg_restore: error")]
        print(f"restore into a throwaway Postgres: {len(errors)} error(s)")
        for line in errors:
            print("   ", line[:160])
        restored = {}
        for table in expected:
            out = subprocess.run(["docker", "exec", name, "psql", "-U", "postgres", "-d", "verify", "-tA", "-c",
                                  f'select count(*) from public."{table}"'], capture_output=True, text=True)
            restored[table] = int(out.stdout.strip()) if out.stdout.strip().isdigit() else None
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    missing = [t for t, n in restored.items() if n is None]
    differ = {t: (expected[t], restored[t]) for t in expected if restored[t] is not None and restored[t] != expected[t]}
    print(f"tables restored: {len(expected) - len(missing)}/{len(expected)}; rows: {sum(n or 0 for n in restored.values())}")
    print("missing tables:", missing or "none", "| row-count differences (source, restored):", differ or "none")
    return not missing and not differ


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the public schema and prove it restores.")
    parser.add_argument("--label", default="manual", help="short note in the file name, e.g. before-<migration>")
    parser.add_argument("--dir", type=Path, default=Path.home() / "Documents" / "ai20k-db-backups")
    parser.add_argument("--no-verify", action="store_true", help="skip the restore check (not recommended)")
    args = parser.parse_args()

    raw = get_settings().database_url
    if not raw.startswith(("postgresql://", "postgres://")):
        print("the backup script is for PostgreSQL; DATABASE_URL points elsewhere", file=sys.stderr)
        return 1
    url = make_url(raw)
    engine = create_engine(raw.replace("postgresql://", "postgresql+psycopg://", 1).replace("postgres://", "postgresql+psycopg://", 1))
    args.dir.mkdir(parents=True, exist_ok=True)
    file_name = f"ai20k-public-{datetime.datetime.now():%Y%m%d-%H%M%S}-{args.label}.dump"
    before = table_counts(engine)
    print(f"source {url.host}/{url.database}: {len(before)} tables, {sum(before.values())} rows")
    started = time.perf_counter()
    result = dump(url, args.dir, file_name)
    target = args.dir / file_name
    if result.returncode != 0 or not target.exists():
        print("pg_dump failed:", result.stderr[-800:].replace(url.password or "\0", "***"), file=sys.stderr)
        return 1
    print(f"dump written in {time.perf_counter() - started:.0f}s: {target} ({target.stat().st_size / 1e6:.2f} MB)")
    if args.no_verify:
        print("restore check skipped")
        return 0
    ok = verify(args.dir, file_name, before)
    print("RESULT:", "BACKUP VERIFIED" if ok else "BACKUP NOT VERIFIED: check the differences above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
