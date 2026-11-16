"""Deactivate catalog models whose ids do not exist on the provider.

Run without arguments for a read-only preview. ``--apply`` sets
``models.status = 'inactive'`` (rows are kept), after writing the previous
statuses to ``tmp/`` so ``--restore <file>`` can put them back.

Checked 2026-10-07 against GET /v1/models for the project's OpenAI key:
``gpt-5.6-luna-pro`` and ``gpt-6-luna-pro`` do not exist (Luna has no -pro
tier; the real ids gpt-5.6-luna and gpt-6-luna already have their own rows).
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
from src.shared.models.gateway import Model

MISSING_MODEL_NAMES = ("gpt-5.6-luna-pro", "gpt-6-luna-pro")


async def run(apply: bool) -> None:
    engine = create_engine_from_settings(None)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        rows = (await session.execute(
            select(Model.id, Model.name, Model.status).where(Model.name.in_(MISSING_MODEL_NAMES))
        )).all()
        for model_id, name, status in rows:
            print(f"{'*' if status != 'inactive' else ' '} {status:8} -> inactive  {name} ({model_id})")
        changed = [{"id": model_id, "status": status} for model_id, _, status in rows if status != "inactive"]
        print(f"\n{len(changed)} model(s) to deactivate.")
        if not apply:
            print("Preview only. Re-run with --apply to write.")
        elif changed:
            path = PROJECT_ROOT / "tmp" / f"model-status-backup-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(changed, indent=2), encoding="utf-8")
            await session.execute(
                update(Model).where(Model.id.in_([item["id"] for item in changed])).values(status="inactive")
            )
            await session.commit()
            print(f"Applied. Backup: {path}")
    await engine.dispose()


async def restore(backup_path: Path) -> None:
    items = json.loads(backup_path.read_text(encoding="utf-8"))
    engine = create_engine_from_settings(None)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        for item in items:
            await session.execute(update(Model).where(Model.id == item["id"]).values(status=item["status"]))
        await session.commit()
    await engine.dispose()
    print(f"Restored {len(items)} model status(es).")


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
