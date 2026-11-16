from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.core import Application, utc_now
from src.shared.models.gateway import ApplicationGatewayConfig
from src.shared.models.runtime import GatewayRequestRecord


class ApplicationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, owner_user_id: str, application_id: str) -> Application | None:
        result = await self.session.execute(
            select(Application).where(
                Application.id == application_id,
                Application.owner_user_id == owner_user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Application | None:
        result = await self.session.execute(select(Application).where(Application.slug == slug))
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Application | None:
        result = await self.session.execute(select(Application).where(Application.name == name))
        return result.scalar_one_or_none()

    async def get(self, application_id: str) -> Application | None:
        return await self.session.get(Application, application_id)

    @staticmethod
    def _filters(
        owner_user_id: str,
        *,
        search: str | None,
        environment: str | None,
        status: str | None,
        scope: str,
        pipeline: str | None = "online",
    ) -> list:
        # The Applications pages list online applications; offline pipelines have their own section.
        conditions = [Application.pipeline == pipeline] if pipeline else []
        if scope == "own":
            conditions.append(Application.owner_user_id == owner_user_id)
        if search:
            term = f"%{search.strip()}%"
            conditions.append(Application.name.ilike(term))
        if environment:
            conditions.append(Application.environment == environment)
        if status:
            conditions.append(Application.status == status)
        return conditions

    async def count(
        self,
        owner_user_id: str,
        *,
        search: str | None = None,
        environment: str | None = None,
        status: str | None = None,
        scope: str = "own",
        pipeline: str | None = "online",
    ) -> int:
        conditions = self._filters(
            owner_user_id,
            search=search,
            environment=environment,
            status=status,
            scope=scope,
            pipeline=pipeline,
        )
        return int(await self.session.scalar(select(func.count()).select_from(Application).where(*conditions)) or 0)

    async def list_filtered(
        self,
        owner_user_id: str,
        *,
        offset: int,
        limit: int,
        search: str | None = None,
        environment: str | None = None,
        status: str | None = None,
        scope: str = "own",
        pipeline: str | None = "online",
    ) -> list[Application]:
        conditions = self._filters(
            owner_user_id,
            search=search,
            environment=environment,
            status=status,
            scope=scope,
            pipeline=pipeline,
        )
        rows = await self.session.scalars(
            select(Application)
            .where(*conditions)
            .order_by(Application.created_at, Application.slug)
            .offset(offset)
            .limit(limit)
        )
        return list(rows)

    async def gateway_config(self, application_id: str) -> ApplicationGatewayConfig | None:
        return await self.session.scalar(
            select(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application_id)
        )

    async def request_count(self, application_id: str) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(GatewayRequestRecord)
                .where(GatewayRequestRecord.application_id == application_id)
            )
            or 0
        )

    async def count_for_owner(self, owner_user_id: str) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Application).where(Application.owner_user_id == owner_user_id)
        )
        return result.scalar_one()

    async def list_for_owner(self, owner_user_id: str, *, offset: int, limit: int) -> list[Application]:
        result = await self.session.execute(
            select(Application)
            .where(Application.owner_user_id == owner_user_id)
            .order_by(Application.created_at, Application.slug)
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def create(self, application: Application) -> Application:
        self.session.add(application)
        await self.session.flush()
        await self.session.refresh(application)
        return application

    async def save(self, application: Application) -> Application:
        application.updated_at = utc_now()
        await self.session.flush()
        await self.session.refresh(application)
        return application
