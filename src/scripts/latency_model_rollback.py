"""Roll back the active latency artifact with an explicit generation CAS."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.backend_ai.repositories.latency import LatencyRepository
from src.shared.database.session import create_engine_from_settings


async def rollback_model(session_factory, *, expected_generation: int, version: str) -> bool:
    async with session_factory() as session:
        return await LatencyRepository(session).rollback(expected_generation, version)


async def _run(expected_generation: int, version: str) -> bool:
    engine = create_engine_from_settings(None)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return await rollback_model(factory, expected_generation=expected_generation, version=version)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expected-generation", required=True, type=int)
    parser.add_argument("--version", required=True, help="previously published version to activate")
    args = parser.parse_args()
    if args.expected_generation < 0:
        parser.error("--expected-generation must be non-negative")
    if not asyncio.run(_run(args.expected_generation, args.version)):
        print("Rollback not applied: generation changed or version is not in rollback history.", file=sys.stderr)
        return 2
    print(f"Activated latency model {args.version}; registry generation advanced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
