"""Create (or refresh) a system pool: the default pool or a task template.

System pools are ownerless, one primary model per tier plus fallbacks.
``default`` is what auto-routing uses for applications without a valid pool;
a template (``code``) is picked by a client with ``model="auto:code"`` or
selected as an application's pool. Pinned models, allow-lists and /v1/models
keep the application's own candidate set.

    python src/scripts/create_default_pool.py [--template code]               # preview
    python src/scripts/create_default_pool.py [--template code] --apply       # write
    python src/scripts/create_default_pool.py [--template code] --deactivate  # undo

Rationale: docs/routing-design-decisions.md sections 5, 6 and 11.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.shared.database.session import create_engine_from_settings
from src.shared.models.gateway import Model, ModelPool, ModelPoolMember, Provider
from src.shared.repositories.routing_catalog import system_pool_purpose

# (role, tier, provider name, model name)
POOLS: dict[str, tuple[str, list[tuple[str, str, str, str]]]] = {
    # GPT-6 Luna, then GPT-6.1 Sol at low effort (mid) and at high effort (hard),
    # as LiteLLM tops a ladder with the same model at higher effort (doc section 13).
    # Fallbacks stay on OpenAI at a comparable rung, as LiteLLM's same-family ladders do.
    "default": ("Default (system)", [
        ("primary", "simple", "openai", "gpt-6-luna"),
        ("primary", "mid", "openai", "gpt-6.1-sol"),
        ("primary", "hard", "openai", "gpt-6.1-sol@high"),
        ("fallback", "simple", "openai", "gpt-5.6-luna"),
        ("fallback", "mid", "openai", "gpt-6-sol"),
        ("fallback", "hard", "openai", "gpt-5.6-sol"),
    ]),
    # Coding (Requesty-style task x size, doc section 11): every primary declares
    # code-relevant capabilities (tool calling, structured output, reasoning) and a
    # 400k+ window for repositories; the large rung stays the general flagship, as
    # LiteLLM keeps one top model across task ladders. Not benchmarked yet.
    "code": ("Coding (system)", [
        ("primary", "simple", "openai", "gpt-5.4-nano"),
        ("primary", "mid", "openai", "gpt-5.4-mini"),
        ("primary", "hard", "openai", "gpt-5.6-sol"),
        ("fallback", "simple", "openrouter", "openrouter/deepseek/deepseek-v4-flash"),
        ("fallback", "mid", "google", "gemini-3.6-flash"),
        ("fallback", "hard", "openrouter", "openrouter/deepseek/deepseek-v4-pro"),
    ]),
}


async def run(template: str, apply: bool, deactivate: bool) -> None:
    name, members = POOLS[template]
    purpose = system_pool_purpose(template)
    engine = create_engine_from_settings(None)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        pool = await session.scalar(
            select(ModelPool).where(ModelPool.owner_user_id.is_(None), ModelPool.purpose == purpose)
        )
        if deactivate:
            if pool is None:
                print(f"No {template} pool.")
            else:
                pool.status = "inactive"
                await session.commit()
                print(f"{name} ({pool.id}) set inactive.")
            await engine.dispose()
            return

        resolved = []
        for role, tier, provider_name, model_name in members:
            model = await session.scalar(
                select(Model).join(Provider, Provider.id == Model.provider_id)
                .where(Provider.name == provider_name, Model.name == model_name)
            )
            problem = ("missing" if model is None else
                       f"status {model.status}" if model.status != "active" else
                       f"tier {model.quality_tier} != {tier}" if model.quality_tier != tier else "")
            print(f"{role:8} {tier:6} {provider_name} / {model_name}" + (f"  !! {problem}" if problem else ""))
            if problem:
                raise SystemExit("Fix the catalog first (src/scripts/reclassify_model_tiers.py); nothing written.")
            resolved.append((role, model))
        print(f"\nExisting {template} pool: {pool.id + ' (' + pool.status + ')' if pool else 'none'}")
        if not apply:
            print("Preview only. Re-run with --apply to write.")
            await engine.dispose()
            return
        if pool is None:
            pool = ModelPool(owner_user_id=None, name=name, purpose=purpose, status="active")
            session.add(pool)
            await session.flush()
        pool.status = "active"
        await session.execute(delete(ModelPoolMember).where(ModelPoolMember.pool_id == pool.id))
        for role, model in resolved:
            session.add(ModelPoolMember(pool_id=pool.id, model_id=model.id, role=role))
        await session.commit()
        print(f"Applied: {name} ({pool.id}) with {len(resolved)} members. Undo with --deactivate.")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", choices=sorted(POOLS), default="default")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="create or refresh the pool")
    group.add_argument("--deactivate", action="store_true", help="set the pool inactive")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run(args.template, args.apply, args.deactivate))


if __name__ == "__main__":
    main()
