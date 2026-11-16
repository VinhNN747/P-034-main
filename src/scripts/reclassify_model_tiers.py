"""Re-label catalog models into the three Custom Pool tiers (simple/mid/hard).

Run without arguments for a read-only preview of every change. ``--apply``
first writes the current labels/capabilities to ``tmp/`` and then updates
``models.quality_tier`` (and the few capability fixes below). ``--restore
<backup.json>`` puts a backup back. Models are matched by provider name and
model name; anything not listed here is left untouched and reported.

Classification rationale: docs/model-tier-classification.md
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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.database.session import create_engine_from_settings
from src.shared.models.gateway import Model, Provider

LIVE_PROVIDER = "openai-live-20260928103943"

# (provider name, model name) -> label. "audio" is deliberately not a tier
# label: an audio model should not be offered as a chat tier.
#
# Placement follows how market routers ladder each vendor's family (see
# docs/routing-design-decisions.md section 10): LiteLLM Auto-Router v2 example
# configs (SIMPLE/MEDIUM/COMPLEX), Azure model router / Bedrock IPR family
# pairs (small vs large of one family), OpenRouter / Not Diamond examples.
# Models no router ladders publicly are placed by their family position and
# are listed as "no market data" in that doc section.
TIERS: dict[tuple[str, str], str] = {
    # Tier 1 - simple: nano/mini/flash-lite/haiku/luna class, cheapest and fastest.
    ("openai", "gpt-5-nano"): "simple",
    ("openai", "gpt-4.1-nano"): "simple",
    ("openai", "gpt-4o-mini"): "simple",  # lowest tier in LiteLLM / RouteLLM examples
    (LIVE_PROVIDER, "gpt-4o-mini"): "simple",
    ("openai", "gpt-4o-mini-2024-07-18"): "simple",
    ("openai", "gpt-5.4-nano"): "simple",
    ("openai", "gpt-3.5-turbo"): "simple",
    ("openai", "gpt-5.6-luna"): "simple",  # LiteLLM SIMPLE (OpenAI ladder Luna < Terra < Sol)
    (LIVE_PROVIDER, "gpt-5.6-luna"): "simple",
    ("openai", "gpt-6-luna"): "simple",
    ("anthropic", "claude-haiku-4.5"): "simple",  # LiteLLM SIMPLE (Haiku < Sonnet < Opus)
    ("openrouter", "openrouter/deepseek/deepseek-v4-flash"): "simple",  # LiteLLM lite ladder SIMPLE
    ("openrouter", "openrouter/google/gemini-3.1-flash-lite"): "simple",
    # Tier 2 - mid: balanced general models with reasoning at mid price.
    ("openai", "gpt-5-mini"): "mid",
    ("openai", "gpt-4.1-mini"): "mid",
    ("openai", "gpt-5.4-mini"): "mid",
    (LIVE_PROVIDER, "gpt-5.6-terra"): "mid",  # LiteLLM MEDIUM ("balanced")
    ("openai", "gpt-6-sol"): "mid",  # everyday reasoning under gpt-6-astra
    ("openai", "gpt-6.1-sol"): "mid",  # low effort; "@high" below is the hard tier
    ("google", "gemini-3.6-flash"): "mid",  # Google ladder flash-lite < flash < pro
    # Tier 3 - hard: flagship reasoning models.
    ("openai", "gpt-6.1-sol@high"): "hard",  # same model at high effort (routing-design-decisions.md section 13)
    ("openai", "gpt-5.6-sol"): "hard",  # LiteLLM COMPLEX
    ("openai", "gpt-6-astra"): "hard",  # top of the GPT-6 family
    ("openai", "gpt-6-luna-pro"): "hard",
    ("openai", "gpt-5.6-luna-pro"): "hard",
    ("openrouter", "openrouter/deepseek/deepseek-v4-pro"): "hard",
    # Not a chat tier.
    ("openai", "gpt-audio-mini"): "audio",
}

# Capabilities added (never removed) so the UI capability filter and the
# router's capability checks see what the tier assumes.
ADD_CAPABILITIES: dict[tuple[str, str], list[str]] = {
    ("openrouter", "openrouter/deepseek/deepseek-v4-pro"): ["reasoning", "structured_output"],
}


async def load(session: AsyncSession) -> list[tuple[Model, Provider]]:
    rows = await session.execute(
        select(Model, Provider).join(Provider, Provider.id == Model.provider_id).order_by(Provider.name, Model.name)
    )
    return list(rows.all())


def planned(model: Model, provider: Provider) -> tuple[str | None, list[str]]:
    key = (provider.name, model.name)
    capabilities = list(model.capabilities_json or [])
    extra = [cap for cap in ADD_CAPABILITIES.get(key, []) if cap not in capabilities]
    return TIERS.get(key), capabilities + extra


async def run(apply: bool) -> None:
    engine = create_engine_from_settings(None)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        rows = await load(session)
        backup, changes, unknown = [], 0, []
        for model, provider in rows:
            label, capabilities = planned(model, provider)
            if label is None:
                unknown.append(f"{provider.name} / {model.name} ({model.quality_tier})")
                continue
            backup.append({"id": model.id, "quality_tier": model.quality_tier, "capabilities": model.capabilities_json})
            changed = label != model.quality_tier or capabilities != list(model.capabilities_json or [])
            marker = "*" if changed else " "
            added = sorted(set(capabilities) - set(model.capabilities_json or []))
            print(f"{marker} {label:6} <- {model.quality_tier:10} {provider.name} / {model.name}"
                  + (f"  +caps {added}" if added else ""))
            changes += changed
            if apply and changed:
                model.quality_tier = label
                model.capabilities_json = capabilities
        for item in unknown:
            print(f"? not in plan, untouched: {item}")
        print(f"\n{changes} model(s) to change, {len(unknown)} not in plan.")
        if not apply:
            print("Preview only. Re-run with --apply to write.")
        elif changes:
            path = PROJECT_ROOT / "tmp" / f"model-tiers-backup-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(backup, indent=2), encoding="utf-8")
            await session.commit()
            print(f"Applied. Backup: {path}")
    await engine.dispose()


async def restore(backup_path: Path) -> None:
    items = json.loads(backup_path.read_text(encoding="utf-8"))
    engine = create_engine_from_settings(None)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        for item in items:
            model = await session.get(Model, item["id"])
            if model is not None:
                model.quality_tier = item["quality_tier"]
                model.capabilities_json = item["capabilities"]
        await session.commit()
    await engine.dispose()
    print(f"Restored {len(items)} model(s) from {backup_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="write the changes (backup first)")
    group.add_argument("--restore", type=Path, help="restore labels/capabilities from a backup file")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(restore(args.restore) if args.restore else run(args.apply))


if __name__ == "__main__":
    main()
