from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.dtos.provider_connection_probe import ConnectionProbeRead
from src.shared.models.gateway import Provider
from src.shared.models.provider_credentials import ProviderCredential, ServiceProviderBinding


class ProviderCredentialRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def provider_with_credential(self, provider_id: str) -> tuple[Provider | None, ProviderCredential | None]:
        row = await self.session.execute(
            select(Provider, ProviderCredential)
            .outerjoin(ProviderCredential, ProviderCredential.provider_id == Provider.id)
            .where(Provider.id == provider_id)
        )
        result = row.one_or_none()
        return result if result is not None else (None, None)

    async def binding_with_provider(
        self, purpose: str
    ) -> tuple[ServiceProviderBinding | None, Provider | None, ProviderCredential | None]:
        row = await self.session.execute(
            select(ServiceProviderBinding, Provider, ProviderCredential)
            .join(Provider, Provider.id == ServiceProviderBinding.provider_id)
            .outerjoin(ProviderCredential, ProviderCredential.provider_id == Provider.id)
            .where(ServiceProviderBinding.purpose == purpose)
        )
        result = row.one_or_none()
        return result if result is not None else (None, None, None)

    async def configured_provider_ids(self) -> set[str]:
        result = await self.session.scalars(
            select(Provider.id)
            .outerjoin(ProviderCredential, ProviderCredential.provider_id == Provider.id)
            .where(
                Provider.status == "active",
                Provider.purpose == "target",
                or_(
                    Provider.kind.in_(("offline", "local")),
                    (ProviderCredential.status == "active") & (ProviderCredential.encrypted_api_key != ""),
                ),
            )
        )
        return set(result)

    async def record_probe_if_current(self, provider_id: str, credential_version: int, result: ConnectionProbeRead) -> bool:
        changed = await self.session.execute(
            update(ProviderCredential)
            .where(
                ProviderCredential.provider_id == provider_id,
                ProviderCredential.version == credential_version,
                ProviderCredential.status == "active",
            )
            .values(
                last_test_status=result.status,
                last_test_at=result.checked_at,
                last_test_credential_version=credential_version,
                last_test_mode=result.mode,
                last_test_model=result.model_name,
                last_test_error_code=result.error_code,
                last_test_latency_ms=result.latency_ms,
            )
        )
        return changed.rowcount == 1
