"""Load an application candidate set without assigning pool member order.

System pools are ownerless: the default pool (``purpose = "system-default"``)
and task templates (``purpose = "system-template:<key>"``, e.g. ``code``).
Auto-routing uses them for an application without a pool, or whose pool does
not have one primary model per tier (a pool saved before that rule), and a
client picks a template with an alias such as ``model="auto:code"``. Pinned
models, allow-lists and /v1/models keep the application's own candidate set.
See docs/routing-design-decisions.md sections 5, 6 and 11.
"""
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.gateway import Model, ModelPool, ModelPoolMember, Provider

DEFAULT_POOL_PURPOSE = "system-default"
TEMPLATE_PURPOSE_PREFIX = "system-template:"
TIER_LABELS = ("simple", "mid", "hard")

Row = tuple[ModelPoolMember | None, Model, Provider]


def system_pool_purpose(template: str | None) -> str:
    return DEFAULT_POOL_PURPOSE if template in (None, "", "default") else f"{TEMPLATE_PURPOSE_PREFIX}{template}"


def is_system_pool(pool: ModelPool) -> bool:
    return pool.owner_user_id is None and (
        pool.purpose == DEFAULT_POOL_PURPOSE or pool.purpose.startswith(TEMPLATE_PURPOSE_PREFIX)
    )


def is_tier_ladder(tiers_by_role: list[tuple[str, str]]) -> bool:
    """(role, quality_tier) pairs: exactly one primary in each tier."""
    primaries = sorted(tier for role, tier in tiers_by_role if role == "primary")
    return primaries == sorted(TIER_LABELS)


def visible_to(owner_user_id: str | None) -> ColumnElement[bool]:
    """Connections a user may see and route to: system ones plus the user's own endpoints.

    ``None`` (no user, or a system pool) means system connections only. A customer's endpoint
    must never reach the shared catalog or another user's requests.
    """
    if owner_user_id is None:
        return Provider.owner_user_id.is_(None)
    return or_(Provider.owner_user_id.is_(None), Provider.owner_user_id == owner_user_id)


async def routing_candidates(session: AsyncSession, pool_id: str | None) -> list[Row]:
    if pool_id is None:
        # No pool: the shared catalog, which holds no customer endpoint.
        rows = await session.execute(
            select(Model, Provider).join(Provider, Provider.id == Model.provider_id).where(visible_to(None))
        )
        return [(None, model, provider) for model, provider in rows.all()]
    rows = await session.execute(
        select(ModelPoolMember, Model, Provider)
        .join(ModelPool, ModelPool.id == ModelPoolMember.pool_id)
        .join(Model, Model.id == ModelPoolMember.model_id)
        .join(Provider, Provider.id == Model.provider_id)
        .where(
            ModelPoolMember.pool_id == pool_id,
            # Pool validation keeps others' endpoints out; this keeps them out of routing anyway.
            or_(Provider.owner_user_id.is_(None), Provider.owner_user_id == ModelPool.owner_user_id),
        )
    )
    return list(rows.all())


async def system_pool_rows(session: AsyncSession, template: str | None = None) -> list[Row]:
    """Members of the active system pool for a template ([] when absent or inactive)."""
    pool_id = await session.scalar(
        select(ModelPool.id)
        .where(ModelPool.owner_user_id.is_(None), ModelPool.purpose == system_pool_purpose(template),
               ModelPool.status == "active")
        .order_by(ModelPool.created_at)
        .limit(1)
    )
    return await routing_candidates(session, pool_id) if pool_id else []


@dataclass(frozen=True)
class RoutingSet:
    """What auto-routing picks from.

    ``rows``: every model the request may use (own candidates plus the system
    pool's). ``auto_roles``: the system pool auto-routing should use (model id
    -> role); empty means the application's own set. ``own_ids``: the
    application's own pool, used when the system pool cannot serve a request
    (None = the whole catalog). ``note``: appended to the routing reason.
    """

    rows: list[Row]
    auto_roles: dict[str, str] = field(default_factory=dict)
    own_ids: frozenset[str] | None = None
    note: str = ""


async def routing_set(session: AsyncSession, pool_id: str | None, *, template: str | None = None) -> RoutingSet:
    rows = await routing_candidates(session, pool_id)
    if pool_id is not None and is_tier_ladder(
        [(member.role, model.quality_tier) for member, model, _ in rows if member is not None]
    ):
        # The application's own ladder (its own pool or a template it selected) wins:
        # the owner chose it, so a client alias does not widen it.
        return RoutingSet(rows, note=f"; auto:{template} not applied (application pool)" if template else "")
    used = template
    auto = await system_pool_rows(session, template) if template else []
    if not auto:
        used = None
        auto = await system_pool_rows(session)
    if not auto:
        return RoutingSet(rows)
    known = {model.id for _, model, _ in rows}
    merged = rows + [row for row in auto if row[1].id not in known]
    reason = f"template pool {used}" if used else "default pool"
    if pool_id is not None:
        reason += " (application pool has no 3-tier ladder)"
    return RoutingSet(
        merged,
        auto_roles={model.id: member.role for member, model, _ in auto if member is not None},
        own_ids=frozenset(known) if pool_id is not None else None,
        note=f"; {reason}",
    )
