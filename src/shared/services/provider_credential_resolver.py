from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.gateway import Provider
from src.shared.models.provider_credentials import ProviderCredential
from src.shared.repositories.provider_credentials import ProviderCredentialRepository
from src.shared.services.credential_crypto import CredentialCrypto, CredentialDecryptionError

CredentialPurpose = Literal["internal_openai", "ocr_google", "routing_jev"]

_BINDING_KINDS: dict[str, str] = {
    "internal_openai": "openai",
    "ocr_google": "google",
    "routing_jev": "typesafe",
}


class CredentialUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("The provider credential is not available.")


class CredentialVersionMismatchError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("The provider credential version changed; reload and retry.")


@dataclass(frozen=True)
class ResolvedCredential:
    provider_id: str
    provider_name: str
    provider_kind: str
    base_url: str | None
    credential_id: str
    version: int
    api_key: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"ResolvedCredential(provider_id={self.provider_id!r}, provider_name={self.provider_name!r}, "
            f"provider_kind={self.provider_kind!r}, credential_id={self.credential_id!r}, "
            f"version={self.version!r}, api_key=<redacted>)"
        )


class ProviderCredentialResolver:
    def __init__(self, session: AsyncSession, crypto: CredentialCrypto | None):
        self.repository = ProviderCredentialRepository(session)
        self.crypto = crypto

    async def resolve_provider(self, provider_id: str) -> ResolvedCredential:
        provider, credential = await self.repository.provider_with_credential(provider_id)
        if provider is None or provider.status != "active" or provider.purpose != "target":
            raise CredentialUnavailableError()
        return self._resolve(provider, credential)

    async def resolve_service(self, purpose: CredentialPurpose) -> ResolvedCredential:
        binding, provider, credential = await self.repository.binding_with_provider(purpose)
        if (
            binding is None
            or provider is None
            or provider.status != "active"
            or provider.kind != _BINDING_KINDS.get(purpose)
            or (purpose == "routing_jev" and provider.purpose != "internal")
        ):
            raise CredentialUnavailableError()
        return self._resolve(provider, credential)

    async def resolve_provider_for_probe(
        self, provider_id: str, *, expected_version: int
    ) -> ResolvedCredential:
        """Resolve an admin probe key without requiring the connection to be active."""
        provider, credential = await self.repository.provider_with_credential(provider_id)
        if provider is None:
            raise CredentialUnavailableError()
        if credential is None or credential.version != expected_version:
            if credential is not None and credential.version != expected_version:
                raise CredentialVersionMismatchError()
            raise CredentialUnavailableError()
        if credential.status != "active" or not credential.encrypted_api_key or self.crypto is None:
            raise CredentialUnavailableError()
        return self._resolve(provider, credential)

    async def configured_provider_ids(self) -> set[str]:
        return await self.repository.configured_provider_ids()

    def _resolve(self, provider: Provider, credential: ProviderCredential | None) -> ResolvedCredential:
        if (
            credential is None
            or credential.status != "active"
            or not credential.encrypted_api_key
            or self.crypto is None
        ):
            raise CredentialUnavailableError()
        try:
            api_key = self.crypto.decrypt(
                provider_id=provider.id,
                credential_id=credential.id,
                version=credential.version,
                ciphertext=credential.encrypted_api_key,
            )
        except CredentialDecryptionError:
            raise CredentialUnavailableError() from None
        return ResolvedCredential(
            provider_id=provider.id,
            provider_name=provider.name,
            provider_kind=provider.kind,
            base_url=provider.base_url,
            credential_id=credential.id,
            version=credential.version,
            api_key=api_key,
        )
