from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.clients.ai_backend_client import AIBackendClient
from src.backend.dtos.provider_credentials import (
    BindingPurpose,
    ConnectionPurpose,
    CredentialSummary,
    ProviderConnectionCreate,
    ProviderConnectionRead,
    ProviderConnectionUpdate,
    ProviderCredentialReplace,
    ServiceProviderBindingRead,
)
from src.shared.dtos.provider_connection_probe import ConnectionProbeInput, ConnectionProbeRead
from src.shared.models.core import utc_now
from src.shared.models.gateway import Provider
from src.shared.models.provider_credentials import ProviderCredential, ServiceProviderBinding
from src.shared.repositories.audit import AuditRepository
from src.shared.services.credential_crypto import CredentialConfigurationError, CredentialCrypto
from src.shared.services.local_endpoints import LocalEndpointError, normalize_local_base_url

_BINDING_KINDS = {"internal_openai": "openai", "ocr_google": "google", "routing_jev": "typesafe"}
_CANONICAL_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com",
}


class ProviderConnectionNotFoundError(Exception):
    pass


class ProviderConnectionConflictError(Exception):
    pass


class ProviderConnectionInvalidError(Exception):
    pass


class ProviderConnectionService:
    def __init__(self, session: AsyncSession, crypto: CredentialCrypto | None):
        self.session = session
        self.crypto = crypto
        self.audit = AuditRepository(session)

    async def list_connections(self) -> list[ProviderConnectionRead]:
        rows = await self.session.execute(
            select(Provider, ProviderCredential)
            .outerjoin(ProviderCredential, ProviderCredential.provider_id == Provider.id)
            .order_by(Provider.display_name, Provider.id)
        )
        return [self._read(provider, credential) for provider, credential in rows.all()]

    async def probe_connection(
        self,
        provider_id: str,
        payload: ConnectionProbeInput,
        actor_user_id: str,
        *,
        ai_client: AIBackendClient,
    ) -> ConnectionProbeRead:
        response = await ai_client.post(
            f"/internal/v1/provider-connections/{provider_id}/probe",
            payload.model_dump(mode="json"),
            actor_user_id=actor_user_id,
        )
        return ConnectionProbeRead.model_validate(response)

    async def create(self, payload: ProviderConnectionCreate, actor_user_id: str) -> ProviderConnectionRead:
        self._validate_kind_purpose(payload.kind, payload.purpose)
        base_url = self._canonical_base_url(payload.kind, payload.base_url)
        api_key = payload.api_key.get_secret_value() if payload.api_key is not None else None
        self._validate_credential_for_kind(payload.kind, api_key)
        if api_key is not None and self.crypto is None:
            raise CredentialConfigurationError()
        if await self.session.scalar(select(Provider.id).where(Provider.name == payload.name)) is not None:
            raise ProviderConnectionConflictError("Provider connection name already exists.")

        provider = Provider(
            id=str(uuid4()),
            name=payload.name,
            display_name=payload.display_name,
            kind=payload.kind,
            purpose=payload.purpose,
            status=payload.status,
            base_url=base_url,
            secret_env_var="",
        )
        self.session.add(provider)
        credential = None
        if api_key is not None:
            credential = self._new_credential(provider, api_key, actor_user_id)
            self.session.add(credential)
        await self.session.flush()
        await self.audit.add_event(
            actor_user_id=actor_user_id,
            action="provider_connection.created",
            resource_type="provider_connection",
            resource_id=provider.id,
            metadata={"kind": provider.kind, "purpose": provider.purpose, "credential_configured": credential is not None},
        )
        await self._commit("Provider connection name already exists.")
        await self.session.refresh(provider)
        return self._read(provider, credential)

    async def update(
        self, provider_id: str, payload: ProviderConnectionUpdate, actor_user_id: str
    ) -> ProviderConnectionRead:
        provider, credential = await self._provider_and_credential(provider_id)
        changes = payload.model_dump(exclude_unset=True)
        if "display_name" in changes and changes["display_name"] is not None:
            provider.display_name = changes["display_name"].strip()
            if not provider.display_name:
                raise ProviderConnectionInvalidError("Display name must not be blank.")
        if "status" in changes and changes["status"] is not None:
            provider.status = changes["status"]
        provider.updated_at = utc_now()
        await self.audit.add_event(
            actor_user_id=actor_user_id,
            action="provider_connection.updated",
            resource_type="provider_connection",
            resource_id=provider.id,
            metadata={"status": provider.status},
        )
        await self._commit("Provider connection could not be updated.")
        await self.session.refresh(provider)
        return self._read(provider, credential)

    async def replace_credential(
        self, provider_id: str, payload: ProviderCredentialReplace, actor_user_id: str
    ) -> ProviderConnectionRead:
        provider = await self.session.get(Provider, provider_id)
        if provider is None:
            raise ProviderConnectionNotFoundError("Provider connection not found.")
        api_key = payload.api_key.get_secret_value()
        self._validate_credential_for_kind(provider.kind, api_key)
        if self.crypto is None:
            raise CredentialConfigurationError()

        existing = await self.session.scalar(
            select(ProviderCredential)
            .where(ProviderCredential.provider_id == provider_id)
            .with_for_update()
        )
        if existing is None:
            if payload.expected_version != 0:
                raise ProviderConnectionConflictError("Credential version changed; reload and retry.")
            credential = self._new_credential(provider, api_key, actor_user_id)
            self.session.add(credential)
            next_version = credential.version
        else:
            if existing.version != payload.expected_version:
                raise ProviderConnectionConflictError("Credential version changed; reload and retry.")
            next_version = existing.version + 1
            ciphertext = self.crypto.encrypt(
                provider_id=provider.id,
                credential_id=existing.id,
                version=next_version,
                api_key=api_key,
            )
            result = await self.session.execute(
                update(ProviderCredential)
                .where(
                    ProviderCredential.id == existing.id,
                    ProviderCredential.version == payload.expected_version,
                )
                .values(
                    encrypted_api_key=ciphertext,
                    encryption_version="fernet-v1",
                    version=next_version,
                    status="active",
                    key_last4=api_key[-4:] if len(api_key) > 4 else None,
                    updated_by=actor_user_id,
                    updated_at=utc_now(),
                    **self._reset_test_values(),
                )
            )
            if result.rowcount != 1:
                await self.session.rollback()
                raise ProviderConnectionConflictError("Credential version changed; reload and retry.")
            credential = await self.session.get(ProviderCredential, existing.id)
        await self.audit.add_event(
            actor_user_id=actor_user_id,
            action="provider_credential.replaced",
            resource_type="provider_connection",
            resource_id=provider.id,
            metadata={"credential_version": next_version, "status": "active"},
        )
        await self._commit("Credential could not be saved.")
        credential = await self.session.scalar(
            select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
        )
        return self._read(provider, credential)

    async def revoke_credential(self, provider_id: str, expected_version: int, actor_user_id: str) -> None:
        provider = await self.session.get(Provider, provider_id)
        if provider is None:
            raise ProviderConnectionNotFoundError("Provider connection not found.")
        credential = await self.session.scalar(
            select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
        )
        if credential is None:
            raise ProviderConnectionNotFoundError("Provider credential not found.")
        next_version = expected_version + 1
        result = await self.session.execute(
            update(ProviderCredential)
            .where(
                ProviderCredential.id == credential.id,
                ProviderCredential.version == expected_version,
            )
            .values(
                encrypted_api_key="",
                version=next_version,
                status="revoked",
                key_last4=None,
                updated_by=actor_user_id,
                updated_at=utc_now(),
                **self._reset_test_values(),
            )
        )
        if result.rowcount != 1:
            await self.session.rollback()
            raise ProviderConnectionConflictError("Credential version changed; reload and retry.")
        await self.audit.add_event(
            actor_user_id=actor_user_id,
            action="provider_credential.revoked",
            resource_type="provider_connection",
            resource_id=provider.id,
            metadata={"credential_version": next_version, "status": "revoked"},
        )
        await self._commit("Credential could not be revoked.")

    async def list_bindings(self) -> list[ServiceProviderBindingRead]:
        output = []
        for purpose in _BINDING_KINDS:
            binding = await self.session.get(ServiceProviderBinding, purpose)
            if binding is None:
                output.append(ServiceProviderBindingRead(
                    purpose=purpose, provider_id=None, provider_name=None, provider_kind=None,
                    provider_status=None, credential=None,
                ))
                continue
            provider, credential = await self._provider_and_credential(binding.provider_id)
            output.append(ServiceProviderBindingRead(
                purpose=purpose,
                provider_id=provider.id,
                provider_name=provider.display_name,
                provider_kind=provider.kind,
                provider_status=provider.status,
                credential=self._credential_summary(credential),
            ))
        return output

    async def set_binding(
        self, purpose: BindingPurpose, provider_id: str | None, actor_user_id: str
    ) -> list[ServiceProviderBindingRead]:
        binding = await self.session.get(ServiceProviderBinding, purpose)
        if provider_id is None:
            if binding is not None:
                await self.session.delete(binding)
        else:
            provider = await self.session.get(Provider, provider_id)
            if provider is None:
                raise ProviderConnectionNotFoundError("Provider connection not found.")
            if provider.kind != _BINDING_KINDS[purpose]:
                raise ProviderConnectionInvalidError("Provider kind does not match the service binding.")
            if purpose == "routing_jev" and provider.purpose != "internal":
                raise ProviderConnectionInvalidError("JEV routing requires an internal TypeSafe connection.")
            if provider.status != "active":
                raise ProviderConnectionInvalidError("An inactive connection cannot be bound to a service.")
            if binding is None:
                binding = ServiceProviderBinding(purpose=purpose, provider_id=provider_id)
                self.session.add(binding)
            else:
                binding.provider_id = provider_id
                binding.updated_at = utc_now()
        await self.audit.add_event(
            actor_user_id=actor_user_id,
            action="service_provider_binding.updated",
            resource_type="service_provider_binding",
            resource_id=purpose,
            metadata={"provider_id": provider_id},
        )
        await self._commit("Service provider binding could not be updated.")
        return await self.list_bindings()

    async def _provider_and_credential(
        self, provider_id: str
    ) -> tuple[Provider, ProviderCredential | None]:
        row = await self.session.execute(
            select(Provider, ProviderCredential)
            .outerjoin(ProviderCredential, ProviderCredential.provider_id == Provider.id)
            .where(Provider.id == provider_id)
        )
        result = row.one_or_none()
        if result is None:
            raise ProviderConnectionNotFoundError("Provider connection not found.")
        return result

    def _new_credential(self, provider: Provider, api_key: str, actor_user_id: str) -> ProviderCredential:
        if self.crypto is None:
            raise CredentialConfigurationError()
        credential_id = str(uuid4())
        return ProviderCredential(
            id=credential_id,
            provider_id=provider.id,
            encrypted_api_key=self.crypto.encrypt(
                provider_id=provider.id,
                credential_id=credential_id,
                version=1,
                api_key=api_key,
            ),
            encryption_version="fernet-v1",
            version=1,
            status="active",
            key_last4=api_key[-4:] if len(api_key) > 4 else None,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )

    @staticmethod
    def _validate_kind_purpose(kind: str, purpose: ConnectionPurpose) -> None:
        if purpose == "target" and kind not in {"openai", "google", "openrouter", "anthropic", "offline", "local"}:
            raise ProviderConnectionInvalidError("This provider kind cannot be used as a target connection.")
        if purpose == "internal" and kind not in {"openai", "google", "typesafe"}:
            raise ProviderConnectionInvalidError("This provider kind cannot be used as an internal connection.")

    @staticmethod
    def _canonical_base_url(kind: str, base_url: str | None) -> str | None:
        expected = _CANONICAL_BASE_URLS.get(kind)
        if expected is not None:
            if base_url not in (None, expected):
                raise ProviderConnectionInvalidError("Provider base URL is not an approved endpoint.")
            return expected
        if kind == "local" and base_url is not None:
            try:
                return normalize_local_base_url(base_url)
            except LocalEndpointError as exc:
                raise ProviderConnectionInvalidError(str(exc)) from exc
        if base_url is not None:
            raise ProviderConnectionInvalidError("This provider kind does not accept a custom base URL.")
        return None

    @staticmethod
    def _validate_credential_for_kind(kind: str, api_key: str | None) -> None:
        if kind in {"offline", "local"} and api_key is not None:
            raise ProviderConnectionInvalidError("Offline and local connections do not accept credentials.")
        if api_key is not None and len(api_key.strip()) < 5:
            raise ProviderConnectionInvalidError("Credential must contain at least 5 characters.")

    @staticmethod
    def _reset_test_values() -> dict:
        return {
            "last_test_status": "untested",
            "last_test_at": None,
            "last_test_credential_version": None,
            "last_test_mode": None,
            "last_test_model": None,
            "last_test_error_code": None,
            "last_test_latency_ms": None,
        }

    @classmethod
    def _credential_summary(cls, credential: ProviderCredential | None) -> CredentialSummary:
        if credential is None:
            return CredentialSummary(configured=False)
        result_current = credential.last_test_credential_version == credential.version
        return CredentialSummary(
            configured=credential.status == "active" and bool(credential.encrypted_api_key),
            status=credential.status,
            version=credential.version,
            key_last4=credential.key_last4,
            last_test_status=credential.last_test_status if result_current else "untested",
            last_test_at=credential.last_test_at if result_current else None,
            last_test_credential_version=credential.last_test_credential_version if result_current else None,
            last_test_mode=credential.last_test_mode if result_current else None,
            last_test_model=credential.last_test_model if result_current else None,
            last_test_error_code=credential.last_test_error_code if result_current else None,
            last_test_latency_ms=credential.last_test_latency_ms if result_current else None,
        )

    @classmethod
    def _read(cls, provider: Provider, credential: ProviderCredential | None) -> ProviderConnectionRead:
        return ProviderConnectionRead(
            id=provider.id,
            name=provider.name,
            display_name=provider.display_name,
            kind=provider.kind,
            purpose=provider.purpose,
            status=provider.status,
            base_url=provider.base_url,
            credential=cls._credential_summary(credential),
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )

    async def _commit(self, conflict_message: str) -> None:
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ProviderConnectionConflictError(conflict_message) from exc
