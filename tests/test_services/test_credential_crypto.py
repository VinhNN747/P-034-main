from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from src.shared.config import Settings
from src.shared.services.credential_crypto import (
    CredentialConfigurationError,
    CredentialCrypto,
    CredentialDecryptionError,
    ensure_credential_crypto,
)


def settings_for_key(key: str = "", **overrides) -> Settings:
    return Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", credential_encryption_key=key, **overrides)


def decrypt_args(values: dict, ciphertext: str) -> dict:
    return {key: values[key] for key in ("provider_id", "credential_id", "version")} | {"ciphertext": ciphertext}


def test_encryption_key_is_loaded_from_environment(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY_FILE", raising=False)
    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:")
    crypto = CredentialCrypto.from_settings(settings)
    values = {"provider_id": "provider-a", "credential_id": "credential-a", "version": 1, "api_key": "provider-secret"}

    assert crypto.decrypt(**decrypt_args(values, crypto.encrypt(**values))) == "provider-secret"


def test_legacy_key_file_environment_variable_is_not_a_key_source(tmp_path: Path, monkeypatch):
    secret_file = tmp_path / "credential.key"
    secret_file.write_text(Fernet.generate_key().decode())
    secret_file.chmod(0o600)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_FILE", str(secret_file))
    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", gateway_execution_mode="live")

    with pytest.raises(CredentialConfigurationError):
        ensure_credential_crypto(settings)


def test_roundtrip_and_ciphertext_not_plaintext():
    key = Fernet.generate_key().decode()
    crypto = CredentialCrypto.from_settings(settings_for_key(key))
    kwargs = {"provider_id": "provider-a", "credential_id": "credential-a", "version": 1, "api_key": "provider-secret"}

    first = crypto.encrypt(**kwargs)
    second = crypto.encrypt(**kwargs)

    assert first != second
    assert "provider-secret" not in first
    assert crypto.decrypt(**decrypt_args(kwargs, first)) == "provider-secret"


def test_wrong_key_and_tamper_fail_without_exposing_plaintext():
    kwargs = {"provider_id": "provider-a", "credential_id": "credential-a", "version": 1, "api_key": "sensitive-key-value"}
    crypto = CredentialCrypto.from_settings(settings_for_key(Fernet.generate_key().decode()))
    ciphertext = crypto.encrypt(**kwargs)
    wrong = CredentialCrypto.from_settings(settings_for_key(Fernet.generate_key().decode()))

    with pytest.raises(CredentialDecryptionError) as error:
        wrong.decrypt(**decrypt_args(kwargs, ciphertext))
    assert "sensitive-key-value" not in str(error.value)
    with pytest.raises(CredentialDecryptionError):
        crypto.decrypt(**decrypt_args(kwargs, ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")))


@pytest.mark.parametrize("field,value", [("provider_id", "provider-b"), ("credential_id", "credential-b"), ("version", 2)])
def test_payload_identity_must_match_database_row(field, value):
    crypto = CredentialCrypto.from_settings(settings_for_key(Fernet.generate_key().decode()))
    values = {"provider_id": "provider-a", "credential_id": "credential-a", "version": 1, "api_key": "provider-secret"}
    ciphertext = crypto.encrypt(**values)

    with pytest.raises(CredentialDecryptionError):
        crypto.decrypt(**decrypt_args({**values, field: value}, ciphertext))


def test_missing_key_allows_offline_but_is_rejected_for_live():
    assert ensure_credential_crypto(settings_for_key()) is None
    with pytest.raises(CredentialConfigurationError):
        ensure_credential_crypto(settings_for_key(gateway_execution_mode="live"))


@pytest.mark.asyncio
async def test_management_and_ai_application_lifespan_reject_live_without_key(monkeypatch):
    from src.shared.core import application

    settings = settings_for_key(gateway_execution_mode="live")
    monkeypatch.setattr(application, "get_settings", lambda: settings)
    app = application.create_application("credential startup test")

    with pytest.raises(CredentialConfigurationError):
        async with app.router.lifespan_context(app):
            pytest.fail("live application started without the shared encryption key")


@pytest.mark.asyncio
async def test_worker_rejects_live_without_key_before_polling():
    from src.backend_ai.services.comparison_service import run_worker

    stop = __import__("asyncio").Event()
    stop.set()
    with pytest.raises(CredentialConfigurationError):
        await run_worker(stop_event=stop, settings=settings_for_key(gateway_execution_mode="live"))


def test_same_key_decrypts_updated_provider_and_internal_credentials():
    crypto = CredentialCrypto.from_settings(settings_for_key(Fernet.generate_key().decode()))
    google = {"provider_id": "google-main", "credential_id": "google-secret", "version": 2, "api_key": "google-key"}
    jev = {"provider_id": "jev-routing", "credential_id": "jev-secret", "version": 1, "api_key": "jev-key"}
    google_ciphertext = crypto.encrypt(**google)
    jev_ciphertext = crypto.encrypt(**jev)

    assert crypto.decrypt(**decrypt_args(google, google_ciphertext)) == "google-key"
    assert crypto.decrypt(**decrypt_args(jev, jev_ciphertext)) == "jev-key"


def test_key_never_appears_in_settings_crypto_repr_or_errors():
    secret = Fernet.generate_key().decode()
    settings = settings_for_key(secret)
    crypto = CredentialCrypto.from_settings(settings)

    assert secret not in repr(settings)
    assert secret not in repr(crypto)
    with pytest.raises(CredentialConfigurationError) as error:
        CredentialCrypto.from_settings(settings_for_key("invalid-sensitive-value"))
    assert "invalid-sensitive-value" not in str(error.value)
