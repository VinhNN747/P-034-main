"""Import legacy provider keys into encrypted PostgreSQL credential rows.

The default invocation is a read-only preview. Keys are read from the process
environment or an explicitly selected dotenv file and are never accepted as
command-line arguments or included in output.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.shared.config import get_settings  # noqa: E402
from src.shared.database.session import create_engine_from_settings  # noqa: E402
from src.shared.models.gateway import Provider  # noqa: E402
from src.shared.models.provider_credentials import ProviderCredential, ServiceProviderBinding  # noqa: E402
from src.shared.services.credential_crypto import CredentialCrypto  # noqa: E402


@dataclass(frozen=True)
class ImportAction:
    provider_id: str
    provider_name: str
    action: str


@dataclass(frozen=True)
class ImportSummary:
    actions: tuple[ImportAction, ...]
    created_credentials: int
    skipped_credentials: int
    created_connections: int
    created_bindings: int
    warnings: tuple[str, ...]


class CredentialImportError(RuntimeError):
    """A secret-safe import configuration error."""


def _canonical_google_value(values: Mapping[str, str], warnings: list[str]) -> str | None:
    google = values.get("GOOGLE_API_KEY", "").strip()
    gemini = values.get("GEMINI_API_KEY", "").strip()
    if google and gemini and google != gemini:
        warnings.append(
            "GOOGLE_API_KEY and GEMINI_API_KEY differ; GEMINI_API_KEY is selected for the canonical Google target, while GOOGLE_API_KEY is reserved for OCR if its binding is empty."
        )
    return gemini or google or None


async def import_credentials(
    session: AsyncSession,
    crypto: CredentialCrypto | None,
    legacy_values: Mapping[str, str],
    *,
    apply: bool,
) -> ImportSummary:
    """Import legacy keys once, preserving existing credentials and bindings.

    ``apply=False`` follows the same decisions as an import but rolls back the
    transaction. The returned summary contains no key material or ciphertext.
    """

    actions: list[ImportAction] = []
    warnings: list[str] = []
    created_credentials = 0
    skipped_credentials = 0
    created_connections = 0
    created_bindings = 0
    newly_configured: dict[str, Provider] = {}

    try:
        values = {name: value.strip() for name, value in legacy_values.items() if value and value.strip()}
        providers = list(
            await session.scalars(select(Provider).order_by(Provider.created_at, Provider.id))
        )
        canonical_google = _canonical_google_value(values, warnings)

        for provider in providers:
            env_var = provider.secret_env_var.strip()
            if not env_var and provider.purpose == "target" and provider.name == provider.kind:
                env_var = {
                    "openai": "OPENAI_API_KEY",
                    "google": "GOOGLE_API_KEY",
                    "openrouter": "OPENROUTER_API_KEY",
                }.get(provider.kind, "")
            if not env_var:
                continue
            api_key = values.get(env_var)
            if provider.kind == "google" and provider.name == "google" and env_var in {
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
            }:
                api_key = canonical_google
            if not api_key or provider.kind in {"offline", "local"}:
                continue
            credential = await session.scalar(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider.id)
            )
            if credential is not None:
                skipped_credentials += 1
                actions.append(ImportAction(provider.id, provider.name, "skipped-existing-credential"))
                continue
            if crypto is None:
                raise CredentialImportError("Credential encryption must be configured before import.")
            _add_credential(session, provider, api_key, crypto)
            created_credentials += 1
            newly_configured[provider.id] = provider
            actions.append(ImportAction(provider.id, provider.name, "credential-created"))

        # JEV was previously configured directly through TYPESAFE_API_KEY.
        jev_key = values.get("TYPESAFE_API_KEY")
        jev_provider: Provider | None = None
        if jev_key:
            jev_provider = await session.scalar(
                select(Provider)
                .where(Provider.kind == "typesafe", Provider.purpose == "internal")
                .order_by(Provider.created_at, Provider.id)
            )
            if jev_provider is None:
                existing_name = await session.scalar(
                    select(Provider).where(Provider.name == "typesafe-routing")
                )
                if existing_name is not None:
                    raise CredentialImportError(
                        "The reserved JEV connection name is already used by an incompatible provider."
                    )
                jev_provider = Provider(
                    id=str(uuid4()),
                    name="typesafe-routing",
                    display_name="TypeSafe JEV routing",
                    kind="typesafe",
                    purpose="internal",
                    status="active",
                    base_url=None,
                    secret_env_var="",
                )
                session.add(jev_provider)
                await session.flush()
                created_connections += 1
                actions.append(ImportAction(jev_provider.id, jev_provider.name, "internal-connection-created"))
            jev_credential = await session.scalar(
                select(ProviderCredential).where(ProviderCredential.provider_id == jev_provider.id)
            )
            if jev_credential is None:
                if crypto is None:
                    raise CredentialImportError("Credential encryption must be configured before import.")
                _add_credential(session, jev_provider, jev_key, crypto)
                created_credentials += 1
                newly_configured[jev_provider.id] = jev_provider
                actions.append(ImportAction(jev_provider.id, jev_provider.name, "credential-created"))
            else:
                skipped_credentials += 1
                actions.append(ImportAction(jev_provider.id, jev_provider.name, "skipped-existing-credential"))

        google_ocr_provider: Provider | None = None
        google_ocr_ready = False
        google_value = values.get("GOOGLE_API_KEY")
        if google_value and canonical_google and google_value != canonical_google:
            current_ocr_binding = await session.get(ServiceProviderBinding, "ocr_google")
            if current_ocr_binding is None:
                google_ocr_provider = await session.scalar(
                    select(Provider).where(Provider.name == "google-ocr-legacy")
                )
                if google_ocr_provider is None:
                    google_ocr_provider = Provider(
                        id=str(uuid4()),
                        name="google-ocr-legacy",
                        display_name="Google OCR legacy connection",
                        kind="google",
                        purpose="internal",
                        status="active",
                        base_url=None,
                        secret_env_var="",
                    )
                    session.add(google_ocr_provider)
                    await session.flush()
                    created_connections += 1
                    actions.append(
                        ImportAction(
                            google_ocr_provider.id,
                            google_ocr_provider.name,
                            "internal-connection-created",
                        )
                    )
                elif google_ocr_provider.kind != "google" or google_ocr_provider.purpose != "internal":
                    raise CredentialImportError(
                        "The reserved OCR connection name is already used by an incompatible provider."
                    )
                google_ocr_credential = await session.scalar(
                    select(ProviderCredential).where(
                        ProviderCredential.provider_id == google_ocr_provider.id
                    )
                )
                if google_ocr_credential is None:
                    if crypto is None:
                        raise CredentialImportError("Credential encryption must be configured before import.")
                    _add_credential(session, google_ocr_provider, google_value, crypto)
                    created_credentials += 1
                    newly_configured[google_ocr_provider.id] = google_ocr_provider
                    google_ocr_ready = True
                    actions.append(
                        ImportAction(
                            google_ocr_provider.id,
                            google_ocr_provider.name,
                            "credential-created",
                        )
                    )
                else:
                    skipped_credentials += 1
                    google_ocr_ready = (
                        google_ocr_credential.status == "active"
                        and bool(google_ocr_credential.encrypted_api_key)
                    )
                    actions.append(
                        ImportAction(
                            google_ocr_provider.id,
                            google_ocr_provider.name,
                            "skipped-existing-credential",
                        )
                    )

        # Bind only configured connections and never replace an operator choice.
        for purpose, kind in (("internal_openai", "openai"), ("ocr_google", "google"), ("routing_jev", "typesafe")):
            binding = await session.get(ServiceProviderBinding, purpose)
            if binding is not None:
                continue
            candidates = [provider for provider in newly_configured.values() if provider.kind == kind]
            if kind == "google" and google_ocr_provider is not None and google_ocr_ready:
                candidates = [google_ocr_provider]
            if not candidates:
                candidates = list(
                    await session.scalars(
                        select(Provider)
                        .join(ProviderCredential, ProviderCredential.provider_id == Provider.id)
                        .where(
                            Provider.kind == kind,
                            Provider.purpose.in_(("target", "internal")),
                            Provider.status == "active",
                            ProviderCredential.status == "active",
                            ProviderCredential.encrypted_api_key != "",
                        )
                        .order_by(Provider.created_at, Provider.id)
                    )
                )
            eligible = [
                provider
                for provider in candidates
                if kind != "typesafe" or provider.purpose == "internal"
            ]
            if eligible:
                session.add(ServiceProviderBinding(purpose=purpose, provider_id=eligible[0].id))
                created_bindings += 1
                actions.append(ImportAction(eligible[0].id, eligible[0].name, f"{purpose}-binding-created"))

        await session.flush()
        summary = ImportSummary(
            actions=tuple(actions),
            created_credentials=created_credentials,
            skipped_credentials=skipped_credentials,
            created_connections=created_connections,
            created_bindings=created_bindings,
            warnings=tuple(warnings),
        )
        if apply:
            await session.commit()
        else:
            await session.rollback()
        return summary
    except Exception:
        await session.rollback()
        raise


def _add_credential(
    session: AsyncSession, provider: Provider, api_key: str, crypto: CredentialCrypto
) -> ProviderCredential:
    credential_id = str(uuid4())
    row = ProviderCredential(
        id=credential_id,
        provider_id=provider.id,
        encrypted_api_key=crypto.encrypt(
            provider_id=provider.id,
            credential_id=credential_id,
            version=1,
            api_key=api_key,
        ),
        encryption_version="fernet-v1",
        version=1,
        status="active",
        key_last4=api_key[-4:] if len(api_key) > 4 else None,
        created_by="legacy-import",
        updated_by="legacy-import",
    )
    session.add(row)
    return row


def _read_legacy_values(env_file: Path | None) -> dict[str, str]:
    values = {name: value for name, value in os.environ.items() if value}
    if env_file is not None:
        file_values = dotenv_values(env_file)
        values.update({name: value for name, value in file_values.items() if value})
    legacy_names = {
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "TYPESAFE_API_KEY",
    }
    return {name: value.strip() for name, value in values.items() if name in legacy_names and value.strip()}


async def _run(*, env_file: Path | None, apply: bool) -> None:
    settings = get_settings()
    crypto = CredentialCrypto.from_settings(settings)
    values = _read_legacy_values(env_file)
    engine = create_engine_from_settings(settings=settings)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            summary = await import_credentials(session, crypto, values, apply=apply)
    finally:
        await engine.dispose()
    mode = "APPLIED" if apply else "DRY RUN (no changes saved)"
    print(mode)
    for action in summary.actions:
        print(f"{action.action}: {action.provider_name} ({action.provider_id})")
    print(
        "Credentials: "
        f"created={summary.created_credentials}, skipped={summary.skipped_credentials}; "
        f"connections={summary.created_connections}; bindings={summary.created_bindings}"
    )
    for warning in summary.warnings:
        print(f"Warning: {warning}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, help="Optional dotenv file with legacy provider keys")
    parser.add_argument("--apply", action="store_true", help="Commit the import; default is dry-run")
    args = parser.parse_args()
    if args.env_file is not None and not args.env_file.is_file():
        parser.error("--env-file must refer to a readable dotenv file")
    try:
        asyncio.run(_run(env_file=args.env_file, apply=args.apply))
    except CredentialImportError as error:
        print(f"Import failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        # Avoid showing driver exceptions that may include connection parameters.
        print(f"Import failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
