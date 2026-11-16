"""Pinned TypeScript SDK exercises the same DB-backed TCP application as Python."""
import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from src.backend_ai.gateway.providers.embeddings import EmbeddingProviderResult
from src.backend_ai.services import embedding_execution_service
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPoolMember, Provider
from tests.test_api.test_openai_compat import openai_env as openai_env
from tests.test_api.test_openai_streaming_tcp import streaming_gateway as streaming_gateway


@pytest.mark.asyncio
async def test_pinned_typescript_sdk_tcp_contracts(streaming_gateway, monkeypatch):
    from cryptography.fernet import Fernet

    from src.backend_ai.routers import openai_embeddings
    from src.backend_ai.services import openai_model_service
    from src.shared.models.provider_credentials import ProviderCredential
    from src.shared.services.credential_crypto import CredentialCrypto

    env = streaming_gateway
    live_embedding_settings = env['settings'].model_copy(update={'gateway_execution_mode': 'live'})
    live_embedding_settings.gateway_openai_enabled = True
    live_embedding_settings.credential_encryption_key = Fernet.generate_key().decode()
    monkeypatch.setattr(openai_embeddings, 'get_settings', lambda: live_embedding_settings)
    monkeypatch.setattr(embedding_execution_service, 'get_settings', lambda: live_embedding_settings)
    monkeypatch.setattr(openai_model_service, 'get_settings', lambda: live_embedding_settings)
    async with env['factory']() as session:
        provider = Provider(name='typescript-embedding', display_name='Fixture', kind='openai', secret_env_var='EMBEDDING_FIXTURE_KEY')
        session.add(provider)
        await session.flush()
        model = Model(provider_id=provider.id, name='text-embedding-3-small', display_name='Embedding', capabilities_json=['embedding'], input_price_per_million=0.02)
        session.add(model)
        await session.flush()
        credential_id = 'typescript-embed-credential'
        crypto = CredentialCrypto.from_settings(live_embedding_settings)
        assert crypto is not None
        session.add(ProviderCredential(
            id=credential_id,
            provider_id=provider.id,
            encrypted_api_key=crypto.encrypt(
                provider_id=provider.id,
                credential_id=credential_id,
                version=1,
                api_key='typescript-fixture-only',
            ),
            version=1,
            status='active',
        ))
        for config in await session.scalars(select(ApplicationGatewayConfig)):
            session.add(ModelPoolMember(pool_id=config.model_pool_id, model_id=model.id))
        await session.commit()
    async def embed(self, payload, validated, **kwargs):
        return EmbeddingProviderResult(payload.model, [[0.1,0.2] for _ in range(validated.input_count)], 3, 1.0)
    monkeypatch.setattr(embedding_execution_service.OpenAIEmbeddingProvider, 'embed', embed)
    package = Path(__file__).resolve().parents[1] / 'sdk_typescript'
    assert (package / 'node_modules/openai').exists(), 'Run npm ci in tests/sdk_typescript first'
    process = await asyncio.create_subprocess_exec('node', '--test', 'contract.test.mjs', cwd=package,
        env={**os.environ, 'GATEWAY_FIXTURE_URL': env['url'], 'GATEWAY_FIXTURE_KEY': env['key'],
            'GATEWAY_FIXTURE_OTHER_KEY': env['other_key'], 'GATEWAY_FIXTURE_REVOKED_KEY': env['revoked']},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    assert process.returncode == 0, output.decode()
