from copy import deepcopy

from sqlalchemy import asc, desc, distinct, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dtos.catalog import ModelCreate, ModelUpdate
from src.backend.services import pool_rules
from src.backend.services.catalog_service import CatalogConflictError, CatalogReferenceError
from src.shared.config import get_settings
from src.shared.dtos.model_catalog import (
    ModelCatalogPage,
    ModelCatalogRead,
    ModelPricingRead,
)
from src.shared.dtos.model_features import ModelPricingMetadata
from src.shared.models.core import Application, utc_now
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPoolMember, Provider
from src.shared.models.provider_credentials import ProviderCredential
from src.shared.repositories.provider_credentials import ProviderCredentialRepository
from src.shared.repositories.routing_catalog import visible_to
from src.shared.services.local_endpoints import is_local_endpoint


class ModelCatalogService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        provider: str | None = None,
        status: str | None = None,
        search: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
        available_only: bool = False,
        viewer_user_id: str | None = None,
    ) -> ModelCatalogPage:
        conditions = self._conditions(provider=provider, status=status, search=search)
        # The shared catalog never lists customer endpoints; a user's available list adds their own.
        conditions.append(visible_to(viewer_user_id if available_only else None))
        if available_only:
            settings = get_settings()
            configured_ids = await ProviderCredentialRepository(self.session).configured_provider_ids()
            provider_rows = (
                await self.session.execute(
                    select(Provider.id, Provider.kind, Provider.base_url).where(Provider.purpose == "target")
                )
            ).all()
            provider_kinds = {provider_id: kind for provider_id, kind, _ in provider_rows}
            local_ids = {provider_id for provider_id, kind, base_url in provider_rows if kind == "local" and base_url}
            callable_ids = pool_rules.callable_provider_ids(settings, configured_ids, provider_kinds, local_ids)
            conditions.extend(
                (
                    Model.status == "active",
                    Provider.status == "active",
                    Provider.purpose == "target",
                    Model.provider_id.in_(callable_ids) if callable_ids else false(),
                )
            )
        total = int(
            await self.session.scalar(select(func.count()).select_from(Model).join(Provider).where(*conditions)) or 0
        )
        used_by = func.count(distinct(Application.id))
        sort_columns = {
            "name": Model.name,
            "provider": Provider.name,
            "status": Model.status,
            "quality_tier": Model.quality_tier,
            "context": Model.context_window,
            "latency": Model.target_latency_ms,
            "input_price": Model.input_price_per_million,
            "output_price": Model.output_price_per_million,
            "used_by": used_by,
        }
        requested_fields = list(dict.fromkeys(field for field in (sort_by or "").split(",") if field in sort_columns))
        requested_dirs = [direction for direction in (sort_dir or "").split(",") if direction in {"asc", "desc"}]
        ordering = [
            desc(sort_columns[field])
            if (requested_dirs[index] if index < len(requested_dirs) else "asc") == "desc"
            else asc(sort_columns[field])
            for index, field in enumerate(requested_fields)
        ]
        ordering.append(asc(Model.id))
        rows = await self.session.execute(
            select(
                Model,
                Provider,
                used_by.label("used_by_count"),
            )
            .join(Provider, Model.provider_id == Provider.id)
            .outerjoin(ModelPoolMember, ModelPoolMember.model_id == Model.id)
            .outerjoin(
                ApplicationGatewayConfig,
                or_(
                    ApplicationGatewayConfig.model_pool_id.is_(None),
                    ApplicationGatewayConfig.model_pool_id == ModelPoolMember.pool_id,
                ),
            )
            .outerjoin(
                Application,
                ApplicationGatewayConfig.application_id == Application.id,
            )
            .where(*conditions)
            .group_by(Model.id, Provider.id)
            .order_by(*ordering)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = [
            self._read(model, provider_row, int(used_by_count)) for model, provider_row, used_by_count in rows.all()
        ]
        return ModelCatalogPage(items=items, page=page, page_size=page_size, total=total)

    async def create(self, payload: ModelCreate) -> ModelCatalogRead:
        provider = await self.session.get(Provider, payload.provider_id)
        if provider is None or provider.status != "active" or provider.purpose != "target":
            raise CatalogReferenceError("Active target provider not found")
        if payload.status == "active":
            raise CatalogReferenceError("New models must be probed before activation")
        duplicate = await self.session.scalar(
            select(Model).where(
                Model.provider_id == payload.provider_id,
                Model.name == payload.name,
            )
        )
        if duplicate is not None:
            raise CatalogConflictError("Model name already exists for provider")
        values = payload.model_dump(mode="json")
        values["capabilities_json"] = values.pop("capabilities")
        values["feature_metadata_json"] = values.pop("feature_metadata")
        values["status"] = "inactive"
        values["health_status"] = "unknown"
        model = Model(**values)
        self.session.add(model)
        try:
            await self.session.flush()
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise CatalogConflictError("Model name already exists for provider") from exc
        await self.session.refresh(model)
        return self._read(model, provider, 0)

    async def update(self, model_id: str, payload: ModelUpdate) -> ModelCatalogRead:
        pair = await self.get(model_id)
        if pair is None:
            raise CatalogReferenceError("Model not found")
        model, provider = pair
        changes = payload.model_dump(exclude_unset=True, mode="json")
        request_profile_reset = (
            ("capabilities" in changes and (changes["capabilities"] or []) != (model.capabilities_json or []))
            or ("context_window" in changes and changes["context_window"] != model.context_window)
            or ("max_output_tokens" in changes and changes["max_output_tokens"] != model.max_output_tokens)
        )
        if "feature_metadata" in changes:
            request_profile_reset = request_profile_reset or self._feature_request_settings(
                changes["feature_metadata"]
            ) != self._feature_request_settings(model.feature_metadata_json)
        if request_profile_reset and changes.get("status") == "active":
            raise CatalogReferenceError("Probe the updated model before activating it")
        self._synchronize_pricing_changes(model, changes)
        requested_status = changes.pop("status", None)
        if requested_status == "active":
            if provider.status != "active" or provider.purpose != "target":
                raise CatalogReferenceError("The model provider is not an active target connection")
            settings = get_settings()
            if is_local_endpoint(provider):
                # A self-hosted server has no credential; a passing probe is the only gate, in both modes.
                if model.health_status != "healthy" or model.last_probe_at is None:
                    raise CatalogReferenceError("Probe the model on its local server before activating it")
            elif settings.gateway_execution_mode == "offline":
                if provider.kind not in {"offline", "local"} or model.health_status != "offline":
                    raise CatalogReferenceError("Probe the offline model before activating it")
            else:
                credential = await self.session.scalar(
                    select(ProviderCredential).where(ProviderCredential.provider_id == provider.id)
                )
                if (
                    credential is None
                    or credential.status != "active"
                    or not credential.encrypted_api_key
                    or model.health_status != "healthy"
                    or model.last_probe_at is None
                    or model.last_probe_credential_version != credential.version
                ):
                    raise CatalogReferenceError(
                        "Probe the model with its current provider credential before activating it"
                    )
            model.status = "active"
        elif requested_status == "inactive":
            model.status = "inactive"
        if "capabilities" in changes:
            model.capabilities_json = changes.pop("capabilities") or []
        if "feature_metadata" in changes:
            model.feature_metadata_json = changes.pop("feature_metadata")
        for field, value in changes.items():
            setattr(model, field, value)
        if request_profile_reset:
            model.status = "inactive"
            model.health_status = "unknown"
            model.last_probe_credential_version = None
        model.updated_at = utc_now()
        await self.session.commit()
        await self.session.refresh(model)
        return self._read(model, provider, 0)

    @staticmethod
    def _synchronize_pricing_changes(model: Model, changes: dict) -> None:
        """Keep editable scalar prices and tiered runtime pricing metadata aligned."""
        if "feature_metadata" in changes:
            metadata = changes["feature_metadata"]
            if metadata is not None and isinstance(metadata.get("pricing"), dict):
                prices = ModelPricingMetadata.model_validate(metadata["pricing"])
                changes["input_price_per_million"] = prices.standard_input_per_million
                changes["output_price_per_million"] = prices.standard_output_per_million
            return

        price_fields = {"input_price_per_million", "output_price_per_million"}
        if not price_fields.intersection(changes):
            return
        current_metadata = model.feature_metadata_json
        if not isinstance(current_metadata, dict) or not isinstance(current_metadata.get("pricing"), dict):
            return

        metadata = deepcopy(current_metadata)
        old_prices = ModelPricingMetadata.model_validate(metadata["pricing"])
        new_input = changes.get("input_price_per_million", model.input_price_per_million)
        new_output = changes.get("output_price_per_million", model.output_price_per_million)
        values = old_prices.model_dump(mode="python")
        input_scale = new_input / old_prices.standard_input_per_million if old_prices.standard_input_per_million else 1
        output_scale = new_output / old_prices.standard_output_per_million if old_prices.standard_output_per_million else 1
        values["standard_input_per_million"] = new_input
        values["standard_output_per_million"] = new_output
        for key in (
            "cache_write_5m_input_per_million", "cache_write_1h_input_per_million",
            "cache_read_input_per_million", "long_context_input_per_million",
        ):
            if values[key] is not None:
                values[key] *= input_scale
        if values["long_context_output_per_million"] is not None:
            values["long_context_output_per_million"] *= output_scale
        metadata["pricing"] = ModelPricingMetadata.model_validate(values).model_dump(mode="json")
        changes["feature_metadata"] = metadata

    @staticmethod
    def _feature_request_settings(metadata: dict | None) -> tuple[dict, dict]:
        if not metadata:
            return {}, {}
        gateway_features = {
            name: {key: detail.get(key) for key in ("support", "conditions")}
            for name, detail in metadata.get("gateway_features", {}).items()
        }
        return metadata.get("constraints", {}), gateway_features

    async def get(self, model_id: str) -> tuple[Model, Provider] | None:
        result = await self.session.execute(
            select(Model, Provider).join(Provider, Model.provider_id == Provider.id).where(Model.id == model_id)
        )
        return result.one_or_none()

    @staticmethod
    def _conditions(*, provider: str | None, status: str | None, search: str | None) -> list:
        conditions = []
        if provider:
            term = f"%{provider.strip()}%"
            conditions.append(or_(Provider.name.ilike(term), Provider.display_name.ilike(term)))
        if status:
            conditions.append(Model.status == status)
        if search:
            term = f"%{search.strip()}%"
            conditions.append(or_(Model.name.ilike(term), Model.display_name.ilike(term)))
        return conditions

    @staticmethod
    def _read(model: Model, provider: Provider, used_by_count: int) -> ModelCatalogRead:
        return ModelCatalogRead(
            id=model.id,
            provider_id=model.provider_id,
            provider=provider.display_name,
            name=model.name,
            display_name=model.display_name,
            status=model.status,
            capabilities=model.capabilities_json or [],
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            feature_metadata=model.feature_metadata_json,
            target_latency_ms=model.target_latency_ms,
            last_probe_latency_ms=model.last_probe_latency_ms,
            quality_tier=model.quality_tier,
            pricing=ModelPricingRead(
                input_per_million=model.input_price_per_million,
                output_per_million=model.output_price_per_million,
                version=model.pricing_version,
            ),
            health_status=model.health_status,
            last_probe_at=model.last_probe_at,
            last_probe_credential_version=model.last_probe_credential_version,
            used_by_count=used_by_count,
        )
