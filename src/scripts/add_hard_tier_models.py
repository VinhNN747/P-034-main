"""Add the OpenAI Sol / Astra models to the catalog as Hard-tier models.

Run without arguments for a read-only preview. ``--apply`` inserts the rows
missing from the catalog (under the provider named ``openai``) and writes
their ids to ``tmp/`` so ``--restore <file>`` can delete exactly those rows.

Prices: https://developers.openai.com/api/docs/pricing (standard tier, read
2026-10-07). Ids checked against GET /v1/models for the project's key.
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

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.shared.database.session import create_engine_from_settings
from src.shared.models.gateway import Model, Provider

PRICING_VERSION = "openai-docs-2026-10"
CAPABILITIES = ["text", "chat", "structured_output", "reasoning", "long_context"]
# name, display name, input $/1M, output $/1M
NEW_MODELS = (
    ("gpt-5.6-sol", "GPT-5.6 Sol", 4.00, 20.00),
    ("gpt-6-sol", "GPT-6 Sol", 2.00, 10.00),
    ("gpt-6.1-sol", "GPT-6.1 Sol", 2.00, 10.00),
    ("gpt-6-astra", "GPT-6 Astra", 10.00, 50.00),
    # Same model as gpt-6.1-sol at high reasoning effort (the provider strips "@high"):
    # the default pool's hard tier, with plain gpt-6.1-sol (low effort) as mid.
    ("gpt-6.1-sol@high", "GPT-6.1 Sol (high effort)", 2.00, 10.00),
)


async def run(apply: bool) -> None:
    engine = create_engine_from_settings(None)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        provider = await session.scalar(select(Provider).where(Provider.name == "openai"))
        if provider is None:
            raise SystemExit("Provider 'openai' not found")
        existing = set(await session.scalars(select(Model.name).where(Model.provider_id == provider.id)))
        added = []
        for name, display, input_price, output_price in NEW_MODELS:
            if name in existing:
                print(f"  exists   {name}")
                continue
            print(f"* add hard {name}  ${input_price:.2f} / ${output_price:.2f}")
            if apply:
                model = Model(
                    provider_id=provider.id, name=name, display_name=display, status="active",
                    capabilities_json=CAPABILITIES, input_price_per_million=input_price,
                    output_price_per_million=output_price, pricing_version=PRICING_VERSION, quality_tier="hard",
                )
                session.add(model)
                await session.flush()
                added.append(model.id)
        print(f"\n{len(added) if apply else sum(n not in existing for n, *_ in NEW_MODELS)} model(s) to add.")
        if not apply:
            print("Preview only. Re-run with --apply to write.")
        elif added:
            path = PROJECT_ROOT / "tmp" / f"added-models-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(added, indent=2), encoding="utf-8")
            await session.commit()
            print(f"Applied. Added ids: {path}")
    await engine.dispose()


async def restore(backup_path: Path) -> None:
    ids = json.loads(backup_path.read_text(encoding="utf-8"))
    engine = create_engine_from_settings(None)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(delete(Model).where(Model.id.in_(ids)))
        await session.commit()
    await engine.dispose()
    print(f"Deleted {len(ids)} added model(s).")


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
