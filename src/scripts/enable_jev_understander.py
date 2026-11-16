"""Switch application gateway configs from the rule understander to jev.

Run without arguments for a read-only preview. ``--apply`` updates every
config still on ``rule`` (bumping its version, like a normal save) and writes
the changed config ids to ``tmp/`` so ``--restore <file>`` can put them back.
jev falls back to the rule understander by itself when it is unavailable.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.shared.database.session import create_engine_from_settings
from src.shared.models.core import Application
from src.shared.models.gateway import ApplicationGatewayConfig


async def run(apply: bool) -> None:
    engine = create_engine_from_settings(None)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        # Explicit columns only, so this also runs before newer migrations.
        table = ApplicationGatewayConfig
        rows = (await session.execute(
            select(table.id, table.default_understander, Application.name)
            .join(Application, Application.id == table.application_id)
            .order_by(Application.name)
        )).all()
        changed = []
        for config_id, understander, name in rows:
            switch = understander == "rule"
            print(f"{'*' if switch else ' '} {understander:5} -> {'jev' if switch else understander:5} {name}")
            if switch:
                changed.append(config_id)
        if apply and changed:
            await session.execute(
                update(table).where(table.id.in_(changed))
                .values(default_understander="jev", version=table.version + 1)
            )
        print(f"\n{len(changed)} of {len(rows)} config(s) to switch.")
        if not apply:
            print("Preview only. Re-run with --apply to write.")
        elif changed:
            path = PROJECT_ROOT / "tmp" / f"understander-backup-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(changed, indent=2), encoding="utf-8")
            await session.commit()
            print(f"Applied. Backup: {path}")
    await engine.dispose()


async def restore(backup_path: Path) -> None:
    ids = json.loads(backup_path.read_text(encoding="utf-8"))
    engine = create_engine_from_settings(None)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        table = ApplicationGatewayConfig
        await session.execute(
            update(table).where(table.id.in_(ids), table.default_understander == "jev")
            .values(default_understander="rule", version=table.version + 1)
        )
        await session.commit()
    await engine.dispose()
    print(f"Restored {len(ids)} config(s) to rule.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--restore", type=Path)
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(restore(args.restore) if args.restore else run(args.apply))


if __name__ == "__main__":
    main()
