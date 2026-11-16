from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.shared.database.base import Base
from src.shared.models.gateway import Model, ModelPool, ModelPoolMember, Provider
from src.shared.models.latency import LatencyObservationRecord
from src.shared.models.provider_credentials import ProviderCredential


class AsyncSessionAdapter:
    """Run the importer against a real sync SQLite session without a worker thread."""

    def __init__(self, session: Session):
        self.session = session

    async def execute(self, *args, **kwargs):
        return self.session.execute(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return self.session.scalar(*args, **kwargs)

    def add(self, value):
        self.session.add(value)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def delete(self, value):
        self.session.delete(value)


@pytest.fixture
def catalog_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        yield AsyncSessionAdapter(session)
    engine.dispose()


@pytest.mark.asyncio
async def test_preview_apply_and_repeat_import_preserve_catalog_values(catalog_session):
    from src.scripts.add_anthropic_models import import_catalog

    preview = await import_catalog(catalog_session, apply=False)
    assert preview.created_provider_id is None
    assert preview.created_model_ids == {}
    assert preview.models_to_create == [
        "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-4-6",
    ]
    assert await catalog_session.scalar(select(func.count()).select_from(Provider)) == 0
    assert await catalog_session.scalar(select(func.count()).select_from(Model)) == 0

    applied = await import_catalog(catalog_session, apply=True)
    assert applied.created_provider_id is not None
    assert len(applied.created_model_ids) == 5
    assert "ANTHROPIC_API_KEY" not in str(applied.receipt)
    provider = await catalog_session.scalar(select(Provider).where(Provider.name == "anthropic"))
    models = list((await catalog_session.execute(select(Model).order_by(Model.name))).scalars())
    assert len(models) == 5
    assert (provider.name, provider.display_name, provider.kind, provider.purpose, provider.status) == (
        "anthropic", "Anthropic", "anthropic", "target", "active",
    )
    assert (provider.base_url, provider.secret_env_var, provider.owner_user_id) == (
        "https://api.anthropic.com", "", None,
    )
    assert provider.created_at is not None and provider.updated_at is not None
    assert {model.name for model in models} == set(applied.created_model_ids)
    expected_latency = {
        "claude-haiku-5-5": (1820, 177),
        "claude-sonnet-5-5": (1350, 96),
        "claude-opus-5-5": (3000, 64),
        "claude-fable-5-1": (3720, 62),
        "claude-sonnet-4-6": (910, 39),
    }
    expected_models = {
        "claude-haiku-5-5": ("Claude Haiku 5.5", "simple", 0.1, 0.5),
        "claude-sonnet-5-5": ("Claude Sonnet 5.5", "mid", 2.0, 10.0),
        "claude-opus-5-5": ("Claude Opus 5.5", "hard", 4.0, 20.0),
        "claude-fable-5-1": ("Claude Fable 5.1", "hard", 10.0, 50.0),
        "claude-sonnet-4-6": ("Claude Sonnet 4.6", "mid", 3.0, 15.0),
    }
    for model in models:
        metadata = model.feature_metadata_json
        display_name, tier, input_price, output_price = expected_models[model.name]
        assert model.id == applied.created_model_ids[model.name]
        assert model.display_name == display_name
        assert (model.provider_id, model.status, model.health_status) == (provider.id, "inactive", "unknown")
        assert model.capabilities_json == [
            "text", "chat", "reasoning", "structured_output", "code_generation", "classification",
            "summarization", "long_context", "tool_use",
        ]
        assert (model.context_window, model.max_output_tokens, model.target_latency_ms) == (1_000_000, 128_000, None)
        assert (model.last_probe_at, model.last_probe_latency_ms, model.last_probe_error) == (None, None, None)
        assert model.last_probe_credential_version is None
        assert (model.input_price_per_million, model.output_price_per_million) == (input_price, output_price)
        assert (model.pricing_version, model.quality_tier) == ("anthropic-docs-2026-10-17", tier)
        assert model.created_at is not None and model.updated_at is not None
        assert metadata["schema_version"] == 1
        assert metadata["provenance"]["checked_on"] == "2026-10-17"
        assert metadata["provenance"]["verification"] == "documentation_only"
        assert metadata["pricing"]["standard_input_per_million"] == input_price
        assert metadata["pricing"]["standard_output_per_million"] == output_price
        assert metadata["pricing"]["batch_discount_fraction"] == 0.5
        assert metadata["latency_reference"]["latency_ms"] == expected_latency[model.name][0]
        assert metadata["latency_reference"]["throughput_tokens_per_second"] == expected_latency[model.name][1]
        assert metadata["latency_reference"]["provider"] == "Anthropic"
        assert metadata["latency_reference"]["source_metric"] == "latency_p50"
        assert metadata["latency_reference"]["metric_interpretation"] == "ttft"
        assert metadata["latency_reference"]["retrieved_on"] == "2026-10-17"
        assert model.input_price_per_million > 0 and model.output_price_per_million > 0
    assert await catalog_session.scalar(select(func.count()).select_from(ProviderCredential)) == 0
    assert await catalog_session.scalar(select(func.count()).select_from(LatencyObservationRecord)) == 0

    original_timestamps = {model.id: (model.created_at, model.updated_at) for model in models}
    models[0].display_name = "Administrator edit"
    edited_updated_at = models[0].updated_at
    catalog_session.session.commit()
    rerun = await import_catalog(catalog_session, apply=True)
    assert rerun.created_provider_id is None
    assert rerun.created_model_ids == {}
    assert len(rerun.skipped) == 6
    assert models[0].display_name == "Administrator edit"
    assert models[0].updated_at == edited_updated_at
    for model in models:
        if model.id != models[0].id:
            assert (model.created_at, model.updated_at) == original_timestamps[model.id]


@pytest.mark.asyncio
async def test_wrong_provider_kind_conflict_rolls_back_without_partial_rows(catalog_session):
    from src.scripts.add_anthropic_models import import_catalog

    catalog_session.add(Provider(
        id="existing-provider", name="anthropic", display_name="Anthropic", kind="openai",
        purpose="target", status="active", base_url="https://api.openai.com/v1", secret_env_var="",
    ))
    await catalog_session.commit()

    result = await import_catalog(catalog_session, apply=True)

    assert result.conflicts
    assert await catalog_session.scalar(select(func.count()).select_from(Provider)) == 1
    assert await catalog_session.scalar(select(func.count()).select_from(Model)) == 0


@pytest.mark.asyncio
async def test_duplicate_model_names_are_reported_as_conflicts(catalog_session):
    from src.scripts.add_anthropic_models import import_catalog

    provider = Provider(
        id="anthropic-provider", name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active", base_url="https://api.anthropic.com", secret_env_var="",
    )
    catalog_session.add(provider)
    for model_id in ("duplicate-1", "duplicate-2"):
        catalog_session.add(Model(
            id=model_id, provider_id=provider.id, name="claude-haiku-5-5", display_name=model_id,
            status="inactive", capabilities_json=[], input_price_per_million=1, output_price_per_million=1,
            pricing_version="test", quality_tier="simple", health_status="unknown",
        ))
    await catalog_session.commit()

    result = await import_catalog(catalog_session, apply=True)

    assert any("Multiple models named 'claude-haiku-5-5'" in conflict for conflict in result.conflicts)
    assert await catalog_session.scalar(select(func.count()).select_from(Model)) == 2


@pytest.mark.asyncio
async def test_restore_refuses_credentials_pools_and_modified_rows(catalog_session):
    from src.scripts.add_anthropic_models import import_catalog, restore_catalog

    applied = await import_catalog(catalog_session, apply=True)
    provider = await catalog_session.scalar(select(Provider).where(Provider.name == "anthropic"))
    model = await catalog_session.scalar(select(Model).where(Model.name == "claude-haiku-5-5"))
    pool = ModelPool(id="pool-1", name="test", purpose="test", status="active")
    credential = ProviderCredential(
        id="credential-1", provider_id=provider.id, encrypted_api_key="ciphertext-only",
        encryption_version="fernet-v1", version=1, status="active", last_test_status="untested",
    )
    member = ModelPoolMember(id="member-1", pool_id=pool.id, model_id=model.id, role="primary")
    catalog_session.add(pool)
    catalog_session.add(credential)
    catalog_session.add(member)
    await catalog_session.commit()

    restore = await restore_catalog(catalog_session, applied.receipt)

    assert restore.conflicts
    assert await catalog_session.scalar(select(func.count()).select_from(Provider)) == 1
    assert await catalog_session.scalar(select(func.count()).select_from(Model)) == 5


@pytest.mark.asyncio
async def test_restore_removes_only_unchanged_rows_created_by_receipt(catalog_session):
    from src.scripts.add_anthropic_models import import_catalog, restore_catalog

    applied = await import_catalog(catalog_session, apply=True)
    restored = await restore_catalog(catalog_session, applied.receipt)

    assert restored.conflicts == []
    assert await catalog_session.scalar(select(func.count()).select_from(Provider)) == 0
    assert await catalog_session.scalar(select(func.count()).select_from(Model)) == 0


@pytest.mark.asyncio
async def test_restore_refuses_a_model_changed_after_import(catalog_session):
    from src.scripts.add_anthropic_models import import_catalog, restore_catalog

    applied = await import_catalog(catalog_session, apply=True)
    model = await catalog_session.scalar(select(Model).where(Model.name == "claude-haiku-5-5"))
    model.display_name = "Edited by admin"
    await catalog_session.commit()

    restored = await restore_catalog(catalog_session, applied.receipt)

    assert restored.conflicts
    assert await catalog_session.scalar(select(func.count()).select_from(Model)) == 5


@pytest.mark.asyncio
async def test_restore_of_models_under_existing_provider_refuses_a_credential(catalog_session):
    from src.scripts.add_anthropic_models import import_catalog, restore_catalog

    provider = Provider(
        id="existing-anthropic", name="anthropic", display_name="Anthropic", kind="anthropic",
        purpose="target", status="active", base_url="https://api.anthropic.com", secret_env_var="",
    )
    catalog_session.add(provider)
    await catalog_session.commit()
    applied = await import_catalog(catalog_session, apply=True)
    catalog_session.add(ProviderCredential(
        id="existing-credential", provider_id=provider.id, encrypted_api_key="ciphertext-only",
        encryption_version="fernet-v1", version=1, status="active", last_test_status="untested",
    ))
    await catalog_session.commit()

    restored = await restore_catalog(catalog_session, applied.receipt)

    assert any("Provider has a credential" in conflict for conflict in restored.conflicts)
    assert await catalog_session.scalar(select(func.count()).select_from(Provider)) == 1
    assert await catalog_session.scalar(select(func.count()).select_from(Model)) == 5


@pytest.mark.asyncio
async def test_import_flushes_provider_before_models_when_foreign_keys_are_enforced(tmp_path: Path):
    from src.scripts.add_anthropic_models import import_catalog

    engine = create_engine(f"sqlite:///{tmp_path / 'foreign-keys.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        result = await import_catalog(AsyncSessionAdapter(session), apply=True)
        assert result.conflicts == []
        assert len(result.created_model_ids) == 5
        assert await AsyncSessionAdapter(session).scalar(select(func.count()).select_from(Provider)) == 1
        assert await AsyncSessionAdapter(session).scalar(select(func.count()).select_from(Model)) == 5
    engine.dispose()
