from dataclasses import dataclass
from time import monotonic

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

AUTHORIZATION_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class AuthorizationProfile:
    roles: tuple[str, ...]
    permissions: frozenset[str]


_authorization_cache: dict[str, tuple[float, AuthorizationProfile]] = {}


def clear_authorization_cache(user_id: str | None = None) -> None:
    """Clear one user's cached profile, or the whole process-local cache."""
    if user_id is None:
        _authorization_cache.clear()
    else:
        _authorization_cache.pop(user_id, None)


async def get_authorization_profile(
    session: AsyncSession, user_id: str
) -> AuthorizationProfile:
    cached = _authorization_cache.get(user_id)
    if cached is not None:
        expires_at, profile = cached
        if monotonic() < expires_at:
            return profile
        _authorization_cache.pop(user_id, None)

    query = """
      select coalesce(array_agg(distinct r.code) filter (where r.code is not null), '{}'),
             coalesce(array_agg(distinct p.code) filter (where p.code is not null), '{}')
      from public.user_roles ur
      join public.roles r on r.id = ur.role_id
      left join public.role_permissions rp on rp.role_id = r.id
      left join public.permissions p on p.id = rp.permission_id
      where ur.user_id = :user_id
    """
    result = await session.execute(text(query), {"user_id": user_id})
    roles, permissions = result.one()
    profile = AuthorizationProfile(tuple(roles), frozenset(permissions))
    _authorization_cache[user_id] = (
        monotonic() + AUTHORIZATION_CACHE_TTL_SECONDS,
        profile,
    )
    return profile
