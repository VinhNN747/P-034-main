"""Exercise embedding claim boundaries with real ORM objects and transactions."""

import pytest
from sqlalchemy import select

from src.backend_ai.gateway.providers.embeddings import EmbeddingProviderResult
from src.backend_ai.services import embedding_execution_service
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPoolMember, Provider
from tests.test_api.test_openai_compat import openai_env as openai_env


@pytest.mark.asyncio
async def test_embedding_claim_retains_catalog_snapshot_after_read_rollback(openai_env, monkeypatch):
    from cryptography.fernet import Fernet

    from src.backend_ai.routers import openai_embeddings
    from src.backend_ai.services import openai_model_service
    from src.shared.models.provider_credentials import ProviderCredential
    from src.shared.services.credential_crypto import CredentialCrypto

    settings = openai_env["settings"]
    settings.gateway_execution_mode = "live"
    settings.gateway_openai_enabled = True
    settings.credential_encryption_key = Fernet.generate_key().decode()
    monkeypatch.setattr(openai_embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embedding_execution_service, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_model_service, "get_settings", lambda: settings)
    monkeypatch.setenv("EMBEDDING_FIXTURE_KEY", "fixture-only")
    async with openai_env["factory"]() as session:
        provider = Provider(
            name="embedding-fixture", display_name="Fixture", kind="openai", secret_env_var="EMBEDDING_FIXTURE_KEY"
        )
        session.add(provider)
        await session.flush()
        model = Model(
            provider_id=provider.id,
            name="text-embedding-3-small",
            display_name="Embedding",
            capabilities_json=["embedding"],
            input_price_per_million=0.02,
        )
        session.add(model)
        await session.flush()
        crypto = CredentialCrypto.from_settings(settings)
        credential = ProviderCredential(
            provider_id=provider.id,
            id="embedding-fixture-credential",
            encrypted_api_key=crypto.encrypt(
                provider_id=provider.id,
                credential_id="embedding-fixture-credential",
                version=1,
                api_key="fixture-database-key",
            ),
            version=1,
            status="active",
        )
        session.add(credential)
        configs = (await session.scalars(select(ApplicationGatewayConfig))).all()
        session.add_all([ModelPoolMember(pool_id=config.model_pool_id, model_id=model.id) for config in configs])
        await session.commit()

    calls = 0

    async def embed(self, payload, validated, *, timeout_seconds):
        nonlocal calls
        calls += 1
        return EmbeddingProviderResult(payload.model, [[0.1, 0.2]], 3, 1.0)

    monkeypatch.setattr(embedding_execution_service.OpenAIEmbeddingProvider, "embed", embed)
    headers = {"Authorization": "Bearer " + openai_env["key"], "Idempotency-Key": "real-orm-embedding"}
    body = {"model": "text-embedding-3-small", "input": "hello", "dimensions": 2}
    first = await openai_env["client"].post("/v1/embeddings", json=body, headers=headers)
    assert first.status_code == 200, first.text
    replay = await openai_env["client"].post("/v1/embeddings", json=body, headers=headers)
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert replay.headers["idempotency-replayed"] == "true"
    assert calls == 1
