from datetime import UTC, timedelta

from sqlalchemy import asc, delete, desc, distinct, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.application_budget import ApplicationBudgetSummary
from src.backend.dtos.applications import (
    ApplicationCreate,
    ApplicationListRead,
    ApplicationModelPreview,
    ApplicationModelsRead,
    ApplicationPage,
    ApplicationRead,
    ApplicationUpdate,
)
from src.backend.dtos.gateway_configuration import GatewayConfigUpsert
from src.backend.repositories.applications import ApplicationRepository
from src.backend.services.catalog_service import CatalogReferenceError
from src.backend.services.gateway_configuration_service import GatewayConfigurationService
from src.shared.models.core import Application, DeletedApplication, utc_now
from src.shared.models.files import FileDeletionOutbox, FileQuotaReservation, GatewayFileRecord
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPoolMember, Provider
from src.shared.models.integration import ApplicationApiKeyRecord
from src.shared.models.openai_responses import StoredOpenAIResponse
from src.shared.models.runtime import GatewayRequestRecord, RuntimeOperationRecord
from src.shared.repositories.audit import AuditRepository
from src.shared.repositories.routing_catalog import visible_to
from src.shared.services.application_budget_service import ApplicationBudgetService


class ApplicationNotFoundError(Exception):
    pass


class ApplicationConflictError(Exception):
    pass


class ApplicationStateError(Exception):
    pass


class ApplicationService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = ApplicationRepository(session)
        self.audit = AuditRepository(session)
        self.configuration = GatewayConfigurationService(session)

    async def _read(self, application: Application, *, include_request_count: bool = True) -> ApplicationRead:
        config = await self.repository.gateway_config(application.id)
        request_count = await self.repository.request_count(application.id) if include_request_count else 0
        gateway_config = None
        budget = None
        if config is not None:
            gateway_config = await self.configuration.get(application.owner_user_id, application.id)
            budget = ApplicationBudgetSummary.model_validate(
                (await ApplicationBudgetService(self.session).snapshot(application.id)).as_dict()
            )
        return ApplicationRead(
            id=application.id,
            slug=application.slug,
            name=application.name,
            description=application.description,
            owner_user_id=application.owner_user_id,
            environment=application.environment,
            status=application.status,
            pipeline=application.pipeline,
            tags=application.tags_json or [],
            created_at=application.created_at,
            updated_at=application.updated_at,
            gateway_config=gateway_config,
            budget=budget,
            request_count=request_count,
            config_version=gateway_config.version if gateway_config else None,
        )

    async def create(
        self,
        owner_user_id: str,
        payload: ApplicationCreate,
        *,
        initialize_gateway: bool = True,
        pipeline: str = "online",
    ) -> ApplicationRead:
        existing_name = await self.repository.get_by_name(payload.name)
        if existing_name is not None:
            raise ApplicationConflictError("Application name already exists")
        existing = await self.repository.get_by_slug(payload.slug)
        if existing is not None:
            raise ApplicationConflictError("Application slug already exists")
        application = Application(
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            owner_user_id=owner_user_id,
            environment=payload.environment,
            status=payload.status,
            pipeline=pipeline,
            tags_json=payload.tags,
        )
        try:
            await self.repository.create(application)
            await self.audit.add_event(
                actor_user_id=owner_user_id,
                action="application.created",
                resource_type="application",
                resource_id=application.id,
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ApplicationConflictError("Application name or slug already exists") from exc

        if initialize_gateway:
            config_payload = payload.gateway_config
            if config_payload is None:
                config_payload = GatewayConfigUpsert()
            try:
                await self.configuration.upsert(owner_user_id, application.id, config_payload)
            except CatalogReferenceError:
                if payload.gateway_config is not None:
                    raise
        application = await self.repository.get(application.id)
        return await self._read(application)

    async def list(
        self,
        owner_user_id: str,
        *,
        page: int,
        page_size: int,
        search: str | None = None,
        environment: str | None = None,
        status: str | None = None,
        scope: str = "own",
        sort_by: str | None = None,
        sort_dir: str | None = None,
    ) -> ApplicationPage:
        total = await self.repository.count(
            owner_user_id,
            search=search,
            environment=environment,
            status=status,
            scope=scope,
        )
        sort_columns = {
            "name": Application.name,
            "created_at": Application.created_at,
            "updated_at": Application.updated_at,
            "environment": Application.environment,
            "status": Application.status,
        }
        requested_fields = list(dict.fromkeys(field for field in (sort_by or "").split(",") if field in sort_columns))
        requested_dirs = [direction for direction in (sort_dir or "").split(",") if direction in {"asc", "desc"}]
        ordering = [
            desc(sort_columns[field]) if (requested_dirs[index] if index < len(requested_dirs) else "asc") == "desc" else asc(sort_columns[field])
            for index, field in enumerate(requested_fields)
        ]
        ordering.append(asc(Application.id))
        rows = await self.session.execute(
            select(Application, ApplicationGatewayConfig)
            .outerjoin(
                ApplicationGatewayConfig,
                ApplicationGatewayConfig.application_id == Application.id,
            )
            .where(
                *self.repository._filters(
                    owner_user_id,
                    search=search,
                    environment=environment,
                    status=status,
                    scope=scope,
                )
            )
            .order_by(*ordering)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        application_rows = rows.all()
        applications = [application for application, _ in application_rows]
        application_ids = [application.id for application in applications]

        model_rows = []
        if application_ids:
            model_rows = (
                await self.session.execute(
                    select(
                        ApplicationGatewayConfig.application_id,
                        Model.id,
                        Model.name,
                        Model.display_name,
                        Provider.name,
                    )
                    .join(Application, Application.id == ApplicationGatewayConfig.application_id)
                    .join(Model, true())
                    .join(Provider, Provider.id == Model.provider_id)
                    .outerjoin(ModelPoolMember, ModelPoolMember.model_id == Model.id)
                    .where(
                        ApplicationGatewayConfig.application_id.in_(application_ids),
                        Model.status == "active",
                        Provider.status == "active",
                        or_(
                            ApplicationGatewayConfig.model_pool_id.is_(None),
                            ModelPoolMember.pool_id == ApplicationGatewayConfig.model_pool_id,
                        ),
                        # A customer endpoint shows only on its owner's applications, and only
                        # through a pool (without one an application uses the shared catalog).
                        or_(
                            Provider.owner_user_id.is_(None),
                            (Provider.owner_user_id == Application.owner_user_id)
                            & ApplicationGatewayConfig.model_pool_id.is_not(None),
                        ),
                    )
                    .order_by(ApplicationGatewayConfig.application_id, Provider.name, Model.name)
                    .distinct()
                )
            ).all()

        previews: dict[str, list[ApplicationModelPreview]] = {}
        for application_id, model_id, model_name, display_name, provider_name in model_rows:
            previews.setdefault(application_id, []).append(
                ApplicationModelPreview(
                    id=model_id,
                    provider=provider_name,
                    name=model_name,
                    display_name=display_name,
                )
            )

        items = []
        for application, config in application_rows:
            models = previews.get(application.id, [])
            items.append(
                ApplicationListRead(
                    id=application.id,
                    slug=application.slug,
                    name=application.name,
                    owner_user_id=application.owner_user_id,
                    is_owned_by_current_user=application.owner_user_id == owner_user_id,
                    environment=application.environment,
                    status=application.status,
                    created_at=application.created_at,
                    updated_at=application.updated_at,
                    routing_objective=config.routing_objective if config else "balanced",
                    budget_ceiling=config.budget_ceiling if config else None,
                    config_version=config.version if config else None,
                    model_preview=models[:3],
                    model_total=len(models),
                )
            )
        return ApplicationPage(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    async def get(self, owner_user_id: str, application_id: str) -> ApplicationRead:
        application = await self.repository.get_by_id(owner_user_id, application_id)
        if application is None:
            raise ApplicationNotFoundError("Application not found")
        return await self._read(application)

    async def models(self, owner_user_id: str, application_id: str) -> ApplicationModelsRead:
        """Return an application's models in one query for the on-demand details drawer."""
        application = await self.repository.get_by_id(owner_user_id, application_id)
        if application is None:
            raise ApplicationNotFoundError("Application not found")

        config = await self.repository.gateway_config(application_id)
        query = (
            select(Model, Provider, func.count(distinct(Application.id)).label("used_by_count"))
            .join(Provider, Provider.id == Model.provider_id)
            .outerjoin(ModelPoolMember, ModelPoolMember.model_id == Model.id)
            .outerjoin(
                ApplicationGatewayConfig,
                (ApplicationGatewayConfig.model_pool_id == ModelPoolMember.pool_id)
                if config and config.model_pool_id
                else ApplicationGatewayConfig.model_pool_id.is_(None),
            )
            .outerjoin(Application, ApplicationGatewayConfig.application_id == Application.id)
            .where(
                Model.status == "active",
                Provider.status == "active",
                visible_to(application.owner_user_id if config and config.model_pool_id else None),
            )
            .group_by(Model.id, Provider.id)
            .order_by(Provider.name, Model.name)
        )
        if config and config.model_pool_id:
            query = query.where(ModelPoolMember.pool_id == config.model_pool_id)

        rows = (await self.session.execute(query)).all()
        from src.backend.services.model_catalog_service import ModelCatalogService

        items = [
            ModelCatalogService._read(model, provider, int(used_by_count))
            for model, provider, used_by_count in rows
        ]
        return ApplicationModelsRead(application_id=application_id, total=len(items), items=items)

    async def update(
        self,
        owner_user_id: str,
        application_id: str,
        payload: ApplicationUpdate,
    ) -> ApplicationRead:
        application = await self.repository.get_by_id(owner_user_id, application_id)
        if application is None:
            raise ApplicationNotFoundError("Application not found")
        if application.status == "archived" and payload.status != "archived":
            raise ApplicationStateError("Archived applications cannot become active")
        changes = payload.model_dump(exclude_unset=True)
        if "name" in changes and changes["name"] != application.name:
            existing = await self.repository.get_by_name(changes["name"])
            if existing is not None and existing.id != application.id:
                raise ApplicationConflictError("Application name already exists")
        if "slug" in changes and changes["slug"] != application.slug:
            existing = await self.repository.get_by_slug(changes["slug"])
            if existing is not None and existing.id != application.id:
                raise ApplicationConflictError("Application slug already exists")
        if "tags" in changes:
            application.tags_json = changes.pop("tags")
        for field, value in changes.items():
            setattr(application, field, value)
        try:
            await self.repository.save(application)
            await self.audit.add_event(
                actor_user_id=owner_user_id,
                action="application.updated",
                resource_type="application",
                resource_id=application.id,
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ApplicationConflictError("Application name or slug already exists") from exc
        return await self._read(application)

    async def delete(self, owner_user_id: str, application_id: str, *, offline_pipeline: bool = False) -> None:
        """``offline_pipeline``: the caller is OfflinePipelineService, which also removes the pipeline's pool."""
        application = await self.session.scalar(
            select(Application)
            .where(Application.id == application_id, Application.owner_user_id == owner_user_id)
            .with_for_update()
        )
        if application is None:
            raise ApplicationNotFoundError("Application not found")
        if application.pipeline == "offline" and not offline_pipeline:
            raise ApplicationStateError("This is an offline pipeline; delete it under Offline pipelines.")
        try:
            self.session.add(
                DeletedApplication(
                    application_id=application.id,
                    owner_user_id=application.owner_user_id,
                    name=application.name,
                    deleted_at=utc_now(),
                )
            )

            files = list(
                await self.session.scalars(
                    select(GatewayFileRecord).where(
                        GatewayFileRecord.application_id == application.id,
                        GatewayFileRecord.lifecycle != "deleted",
                    )
                )
            )
            for file_record in files:
                if await self.session.get(FileDeletionOutbox, file_record.id) is None:
                    self.session.add(
                        FileDeletionOutbox(
                            file_id=file_record.id,
                            storage_backend=file_record.storage_backend,
                            storage_key=file_record.storage_key,
                        )
                    )

            reservations = list(
                await self.session.scalars(
                    select(FileQuotaReservation).where(FileQuotaReservation.application_id == application.id)
                )
            )
            for reservation in reservations:
                if await self.session.get(FileDeletionOutbox, reservation.id) is None:
                    reservation_expires_at = reservation.expires_at
                    if reservation_expires_at.tzinfo is None:
                        reservation_expires_at = reservation_expires_at.replace(tzinfo=UTC)
                    self.session.add(
                        FileDeletionOutbox(
                            file_id=reservation.id,
                            storage_backend=reservation.storage_backend,
                            storage_key=f"{application.id}/{reservation.id}",
                            available_at=max(reservation_expires_at, utc_now()) + timedelta(minutes=10),
                        )
                    )

            key_ids = list(
                await self.session.scalars(
                    select(ApplicationApiKeyRecord.id).where(
                        ApplicationApiKeyRecord.application_id == application.id
                    )
                )
            )
            if key_ids:
                await self.session.execute(
                    update(GatewayRequestRecord)
                    .where(
                        GatewayRequestRecord.application_id == application.id,
                        GatewayRequestRecord.application_api_key_id.in_(key_ids),
                    )
                    .values(application_api_key_id=None)
                )

            await self.session.execute(
                delete(RuntimeOperationRecord).where(RuntimeOperationRecord.application_id == application.id)
            )
            await self.session.execute(
                delete(StoredOpenAIResponse).where(StoredOpenAIResponse.application_id == application.id)
            )
            await self.session.execute(
                delete(FileQuotaReservation).where(FileQuotaReservation.application_id == application.id)
            )
            await self.session.execute(
                delete(GatewayFileRecord).where(GatewayFileRecord.application_id == application.id)
            )
            await self.session.execute(
                delete(ApplicationGatewayConfig).where(ApplicationGatewayConfig.application_id == application.id)
            )
            if key_ids:
                await self.session.execute(
                    delete(ApplicationApiKeyRecord).where(ApplicationApiKeyRecord.id.in_(key_ids))
                )

            await self.audit.add_event(
                actor_user_id=owner_user_id,
                action="application.deleted",
                resource_type="application",
                resource_id=application.id,
            )
            await self.session.delete(application)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
