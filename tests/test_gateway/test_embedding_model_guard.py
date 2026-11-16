from types import SimpleNamespace

from src.backend_ai.services.gateway_routing_service import DatabaseRoutingService, supports_text_generation
from src.shared.dtos.gateway import RequestConstraints


def test_embedding_only_models_are_not_generation_candidates_or_fallbacks():
    embedding_only = SimpleNamespace(
        capabilities_json=["embedding"], status="active", id="embed-id", name="embed-model"
    )
    chat_model = SimpleNamespace(
        capabilities_json=["chat", "embedding"], status="active", id="chat-id", name="chat-model"
    )
    provider = SimpleNamespace(status="active", name="openai", kind="openai", id="provider-id", purpose="target")
    no_required_capabilities = SimpleNamespace(required_capabilities=[])

    assert supports_text_generation(embedding_only) is False
    assert supports_text_generation(chat_model) is True
    assert DatabaseRoutingService._eligible(
        embedding_only, provider, no_required_capabilities, RequestConstraints()
    ) is False
    assert DatabaseRoutingService._eligible(
        chat_model, provider, no_required_capabilities, RequestConstraints()
    ) is True
