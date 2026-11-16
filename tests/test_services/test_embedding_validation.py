import pytest
from pydantic import ValidationError

from src.backend_ai.dtos.openai_embeddings import EmbeddingRequest
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.embedding_validation import (
    EMBEDDING_PROFILES,
    validate_embedding_input,
)

SMALL = EMBEDDING_PROFILES["text-embedding-3-small"]


@pytest.fixture(autouse=True)
def local_test_tokenizer(monkeypatch):
    class Encoding:
        n_vocab = 200_000

        @staticmethod
        def encode(value, *, allowed_special, disallowed_special):
            return [0] * len(value)

    monkeypatch.setattr("src.backend_ai.services.embedding_validation._get_tokenizer", lambda _name: Encoding())


@pytest.mark.parametrize(
    ("value", "count", "tokens"),
    [
        ("hello 世界", 1, None),
        (["hello", "world"], 2, None),
        ([1, 2, 3], 1, 3),
        ([[1, 2], [3]], 2, 3),
    ],
)
def test_four_input_shapes_preserve_order(value, count, tokens):
    result = validate_embedding_input(EmbeddingRequest(model=SMALL.model_id, input=value), SMALL)

    assert result.provider_input == value
    assert result.input_count == count
    if tokens is not None:
        assert result.estimated_input_tokens == tokens


@pytest.mark.parametrize(
    "value",
    [[], "", ["ok", ""], [True], [1, False], [1.0], [None], ["text", 2], [[1], "text"], [[1], []], [[-1]], [[0] * (SMALL.max_input_tokens + 1)]],
)
def test_invalid_inputs_are_rejected(value):
    request = EmbeddingRequest(model=SMALL.model_id, input=value)
    with pytest.raises(OpenAIAPIError) as error:
        validate_embedding_input(request, SMALL)
    assert error.value.status_code == 400
    assert error.value.param == "input"


def test_token_boundaries_and_flat_ids_are_one_input():
    exact = validate_embedding_input(
        EmbeddingRequest(model=SMALL.model_id, input="x" * SMALL.max_input_tokens), SMALL
    )
    assert exact.input_count == 1
    assert exact.estimated_input_tokens == SMALL.max_input_tokens
    with pytest.raises(OpenAIAPIError):
        validate_embedding_input(
            EmbeddingRequest(model=SMALL.model_id, input="x" * (SMALL.max_input_tokens + 1)), SMALL
        )


def test_batch_limit_and_summed_token_limit():
    exact = validate_embedding_input(
        EmbeddingRequest(model=SMALL.model_id, input=["x"] * 128), SMALL
    )
    assert exact.input_count == 128
    with pytest.raises(OpenAIAPIError) as error:
        validate_embedding_input(EmbeddingRequest(model=SMALL.model_id, input=["x"] * 129), SMALL)
    assert error.value.param == "input"

    exact_total = validate_embedding_input(
        EmbeddingRequest(model=SMALL.model_id, input=["x" * 2343] * 128), SMALL
    )
    assert exact_total.estimated_input_tokens == 299_904
    with pytest.raises(OpenAIAPIError):
        validate_embedding_input(EmbeddingRequest(model=SMALL.model_id, input=["x" * 2344] * 128), SMALL)


@pytest.mark.parametrize("dimensions", [True, None, 1.5, "2"])
def test_dimensions_are_strict_and_null_is_not_accepted(dimensions):
    with pytest.raises(ValidationError):
        EmbeddingRequest(model=SMALL.model_id, input="hi", dimensions=dimensions)


@pytest.mark.parametrize("dimensions", [0, 1537])
def test_dimensions_must_be_inside_profile_range(dimensions):
    with pytest.raises(OpenAIAPIError) as error:
        validate_embedding_input(
            EmbeddingRequest(model=SMALL.model_id, input="hi", dimensions=dimensions), SMALL
        )
    assert error.value.param == "dimensions"


def test_encoding_and_user_are_strict():
    with pytest.raises(ValidationError):
        EmbeddingRequest(model=SMALL.model_id, input="hi", encoding_format=None)
    with pytest.raises(ValidationError):
        EmbeddingRequest(model=SMALL.model_id, input="hi", user=None)
    with pytest.raises(ValidationError):
        EmbeddingRequest(model=SMALL.model_id, input="hi", user="x" * 257)


@pytest.mark.asyncio
async def test_embedding_model_visibility_capability_and_profile(monkeypatch):
    from types import SimpleNamespace

    from src.backend_ai.services import embedding_validation

    model = SimpleNamespace(
        id="model-id", name="text-embedding-3-small", capabilities_json=["embedding"],
        input_price_per_million=0.02, pricing_version="price-v1",
    )
    provider = SimpleNamespace(name="openai", kind="openai")
    class FakeModelService:
        def __init__(self, _session):
            pass

        async def list_visible(self, _principal):
            from src.backend_ai.dtos.openai_compat import ModelCard, ModelList
            return ModelList(data=[ModelCard(id=model.name, owned_by="gateway-pinned")])

        async def visible_candidates(self, _principal):
            return [(model, provider)]

    monkeypatch.setattr(
        embedding_validation,
        "OpenAIModelService",
        FakeModelService,
    )
    resolved = await embedding_validation.resolve_embedding_model(
        object(), SimpleNamespace(application_id="app"), model.name
    )
    assert resolved == (model, provider, SMALL)
