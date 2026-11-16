"""Authenticated encryption for provider and internal-service credentials."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from cryptography.fernet import Fernet

from src.shared.config import Settings


class CredentialConfigurationError(RuntimeError):
    """A generic, secret-safe credential encryption configuration error."""

    def __init__(self) -> None:
        super().__init__("Credential encryption is not configured correctly.")


class CredentialDecryptionError(RuntimeError):
    """Ciphertext could not be authenticated or did not match its database row."""

    def __init__(self) -> None:
        super().__init__("The stored credential could not be decrypted.")


@dataclass(repr=False, slots=True)
class CredentialCrypto:
    _fernet: Fernet = field(repr=False)

    def __repr__(self) -> str:
        return "CredentialCrypto(<redacted>)"

    @classmethod
    def from_settings(cls, settings: Settings) -> CredentialCrypto | None:
        raw_key = settings.credential_encryption_key.strip()
        if not raw_key:
            return None

        try:
            fernet = Fernet(raw_key.encode("ascii"))
        except Exception:
            raise CredentialConfigurationError() from None
        return cls(fernet)

    def encrypt(self, *, provider_id: str, credential_id: str, version: int, api_key: str) -> str:
        payload = {
            "schema": 1,
            "provider_id": provider_id,
            "credential_id": credential_id,
            "version": version,
            "api_key": api_key,
        }
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self._fernet.encrypt(encoded).decode("ascii")

    def decrypt(
        self,
        *,
        provider_id: str,
        credential_id: str,
        version: int,
        ciphertext: str,
    ) -> str:
        try:
            payload = json.loads(self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8"))
            if (
                payload.get("schema") != 1
                or payload.get("provider_id") != provider_id
                or payload.get("credential_id") != credential_id
                or payload.get("version") != version
                or not isinstance(payload.get("api_key"), str)
                or not payload["api_key"]
            ):
                raise ValueError
            return payload["api_key"]
        except Exception:
            raise CredentialDecryptionError() from None


def ensure_credential_crypto(settings: Settings) -> CredentialCrypto | None:
    crypto = CredentialCrypto.from_settings(settings)
    if settings.gateway_execution_mode == "live" and crypto is None:
        raise CredentialConfigurationError()
    return crypto
