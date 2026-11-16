import base64
import struct
from types import SimpleNamespace

import pytest

from src.backend_ai.dtos.openai_embeddings import EmbeddingRequest
from src.backend_ai.gateway.providers.embeddings import (
    EmbeddingProviderError,
    EmbeddingProviderResult,
    OpenAIEmbeddingProvider,
    serialize_embeddings,
)
from src.backend_ai.services.embedding_validation import (
    EMBEDDING_PROFILES,
    ValidatedEmbeddingInput,
)


@pytest.mark.asyncio
async def test_provider_forwards_input_dimensions_and_user(monkeypatch):
    captured = {}

    class FakeClient:
        embeddings = SimpleNamespace(create=None)

        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            async def create(**kwargs):
                captured["request"] = kwargs
                return SimpleNamespace(
                    model="text-embedding-3-small",
                    data=[
                        SimpleNamespace(index=1, embedding=[0.25, -1.0]),
                        SimpleNamespace(index=0, embedding=[1.5, 2.0]),
                    ],
                    usage=SimpleNamespace(prompt_tokens=11, total_tokens=11),
                )
            self.embeddings = SimpleNamespace(create=create)
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("openai.AsyncOpenAI", FakeClient)
    provider = OpenAIEmbeddingProvider(api_key="test", base_url="https://provider.test/v1")
    request = EmbeddingRequest(
        model="text-embedding-3-small", input=["first", "second"], dimensions=2, user="user-7"
    )
    validated = ValidatedEmbeddingInput(["first", "second"], 2, 11, 2)

    result = await provider.embed(request, validated, timeout_seconds=7)

    assert captured["client"]["max_retries"] == 0
    assert captured["client"]["timeout"] == 7
    assert captured["client"]["base_url"] == "https://provider.test/v1"
    assert captured["request"] == {
        "model": request.model,
        "input": validated.provider_input,
        "encoding_format": "float",
        "dimensions": 2,
        "user": "user-7",
    }
    assert result.vectors == [[1.5, 2.0], [0.25, -1.0]]
    assert result.input_tokens == 11


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,usage,model",
    [
        ([SimpleNamespace(index=0, embedding=[1.0, 2.0])], None, "text-embedding-3-small"),
        ([SimpleNamespace(index=0, embedding=[1.0, 2.0]), SimpleNamespace(index=0, embedding=[3.0, 4.0])],
         SimpleNamespace(prompt_tokens=3, total_tokens=3), "text-embedding-3-small"),
        ([SimpleNamespace(index=2, embedding=[1.0, 2.0]), SimpleNamespace(index=1, embedding=[3.0, 4.0])],
         SimpleNamespace(prompt_tokens=3, total_tokens=3), "text-embedding-3-small"),
        ([SimpleNamespace(index=0, embedding=[True, 2.0]), SimpleNamespace(index=1, embedding=[3.0, 4.0])],
         SimpleNamespace(prompt_tokens=3, total_tokens=3), "text-embedding-3-small"),
        ([SimpleNamespace(index=0, embedding=[float("nan"), 2.0]), SimpleNamespace(index=1, embedding=[3.0, 4.0])],
         SimpleNamespace(prompt_tokens=3, total_tokens=3), "text-embedding-3-small"),
        ([SimpleNamespace(index=0, embedding=[1.0]), SimpleNamespace(index=1, embedding=[3.0, 4.0])],
         SimpleNamespace(prompt_tokens=3, total_tokens=3), "text-embedding-3-small"),
        ([SimpleNamespace(index=0, embedding=[1.0, 2.0]), SimpleNamespace(index=1, embedding=[3.0, 4.0])],
         SimpleNamespace(prompt_tokens=3, total_tokens=4), "text-embedding-3-small"),
        ([SimpleNamespace(index=0, embedding=[1.0, 2.0]), SimpleNamespace(index=1, embedding=[3.0, 4.0])],
         SimpleNamespace(prompt_tokens=3, total_tokens=3), "some-other-model"),
    ],
)
async def test_malformed_provider_response_is_rejected(monkeypatch, data, usage, model):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            self.embeddings = SimpleNamespace(
                create=lambda **_kwargs: _response(data, usage, model)
            )
            return self

        async def __aexit__(self, *_args):
            return None

    async def create_response(data, usage, model):
        return SimpleNamespace(data=data, usage=usage, model=model)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeClient)
    request = EmbeddingRequest(model="text-embedding-3-small", input=["a", "b"], dimensions=2)
    validated = ValidatedEmbeddingInput(["a", "b"], 2, 2, 2)
    with pytest.raises(EmbeddingProviderError) as error:
        await OpenAIEmbeddingProvider(api_key="test").embed(request, validated, timeout_seconds=1)
    assert error.value.code == "invalid_provider_response"


async def _response(data, usage, model):
    return SimpleNamespace(data=data, usage=usage, model=model)


def test_float_and_base64_use_identical_float32_values():
    result = EmbeddingProviderResult(
        model="text-embedding-3-small", vectors=[[0.1, -2.25]], input_tokens=4, latency_ms=1.0
    )
    floats = serialize_embeddings(result, "float")
    encoded = serialize_embeddings(result, "base64")

    raw = base64.b64decode(encoded.data[0].embedding, validate=True)
    assert len(raw) == 8
    assert struct.unpack("<2f", raw) == tuple(floats.data[0].embedding)
    assert floats.usage.prompt_tokens == floats.usage.total_tokens == 4
    assert EMBEDDING_PROFILES[result.model].native_dimensions == 1536
