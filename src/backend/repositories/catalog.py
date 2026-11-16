from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.gateway import (
    Model,
    ModelPool,
    ModelPoolMember,
    Provider,
    RoutingPolicy,
)
from src.shared.repositories.routing_catalog import is_system_pool


class CatalogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def provider_by_name(self, name: str) -> Provider | None:
        result = await self.session.execute(select(Provider).where(Provider.name == name))
        return result.scalar_one_or_none()

    async def provider(self, provider_id: str) -> Provider | None:
        return await self.session.get(Provider, provider_id)

    async def model(self, model_id: str) -> Model | None:
        return await self.session.get(Model, model_id)

    async def model_by_provider_and_name(self, provider_id: str, name: str) -> Model | None:
        result = await self.session.execute(select(Model).where(Model.provider_id == provider_id, Model.name == name))
        return result.scalar_one_or_none()

    async def pool(self, pool_id: str) -> ModelPool | None:
        return await self.session.get(ModelPool, pool_id)

    async def pool_by_name(self, name: str, owner_user_id: str) -> ModelPool | None:
        result = await self.session.execute(select(ModelPool).where(ModelPool.name == name, ModelPool.owner_user_id == owner_user_id))
        return result.scalar_one_or_none()

    async def pool_members(self, pool_id: str) -> list[ModelPoolMember]:
        result = await self.session.execute(
            select(ModelPoolMember)
            .where(ModelPoolMember.pool_id == pool_id)
            .order_by(ModelPoolMember.model_id)
        )
        return list(result.scalars().all())

    async def policy(self, policy_id: str) -> RoutingPolicy | None:
        return await self.session.get(RoutingPolicy, policy_id)

    async def policy_by_name(self, name: str) -> RoutingPolicy | None:
        result = await self.session.execute(select(RoutingPolicy).where(RoutingPolicy.name == name))
        return result.scalar_one_or_none()

    async def list_providers(self) -> list[Provider]:
        result = await self.session.execute(
            select(Provider).where(Provider.purpose == "target").order_by(Provider.name)
        )
        return list(result.scalars().all())

    async def list_models(self) -> list[Model]:
        result = await self.session.execute(select(Model).order_by(Model.name))
        return list(result.scalars().all())

    async def list_pools(self, owner_user_id: str) -> list[ModelPool]:
        result = await self.session.execute(select(ModelPool).where(ModelPool.owner_user_id == owner_user_id).order_by(ModelPool.name))
        return list(result.scalars().all())

    async def system_pools(self) -> list[ModelPool]:
        """Active ownerless default pool and task templates, readable by every owner."""
        result = await self.session.execute(
            select(ModelPool).where(ModelPool.owner_user_id.is_(None), ModelPool.status == "active").order_by(ModelPool.name)
        )
        return [pool for pool in result.scalars().all() if is_system_pool(pool)]

    async def list_policies(self) -> list[RoutingPolicy]:
        result = await self.session.execute(select(RoutingPolicy).order_by(RoutingPolicy.priority, RoutingPolicy.name))
        return list(result.scalars().all())
