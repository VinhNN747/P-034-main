from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import tiktoken
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_ai.dtos.openai_embeddings import EmbeddingRequest
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.application_auth_service import ApplicationPrincipal
from src.backend_ai.services.openai_model_service import OpenAIModelService
from src.shared.models.gateway import Model, Provider

MAX_EMBEDDING_BATCH_SIZE = 128
MAX_EMBEDDING_REQUEST_TOKENS = 300_000


@dataclass(frozen=True)
class EmbeddingModelProfile:
    model_id: str
    native_dimensions: int
    tokenizer_name: str = "cl100k_base"
    max_input_tokens: int = 8192
    max_total_tokens: int = MAX_EMBEDDING_REQUEST_TOKENS


EMBEDDING_PROFILES = {
    "text-embedding-3-small": EmbeddingModelProfile("text-embedding-3-small", 1536),
    "text-embedding-3-large": EmbeddingModelProfile("text-embedding-3-large", 3072),
}


@dataclass(frozen=True)
class ValidatedEmbeddingInput:
    provider_input: str | list[str] | list[int] | list[list[int]]
    input_count: int
    estimated_input_tokens: int
    expected_dimensions: int


def _invalid(message: str, *, param: str = "input") -> OpenAIAPIError:
    return OpenAIAPIError(400, message, code="invalid_request", param=param)


def validate_embedding_input(
    payload: EmbeddingRequest,
    profile: EmbeddingModelProfile,
) -> ValidatedEmbeddingInput:
    value = payload.input
    encoding = _get_tokenizer(profile.tokenizer_name)
    counts: list[int]
    if type(value) is str:
        if not value:
            raise _invalid("input must not be empty.")
        values: str | list[str] | list[int] | list[list[int]] = value
        counts = [len(encoding.encode(value, allowed_special=set(), disallowed_special=()))]
    elif type(value) is list and value:
        if all(type(item) is str for item in value):
            if any(not item for item in value):
                raise _invalid("Each input string must not be empty.")
            values = value
            counts = [len(encoding.encode(item, allowed_special=set(), disallowed_special=())) for item in value]
        elif all(type(item) is int for item in value):
            values = value
            counts = [_token_id_count(value, encoding.n_vocab)]
        elif all(type(item) is list for item in value):
            if any(not item or any(type(token) is not int for token in item) for item in value):
                raise _invalid("Each token input must be a non-empty list of integer token IDs.")
            values = value
            counts = [_token_id_count(item, encoding.n_vocab) for item in value]
        else:
            raise _invalid("input must be a string or a non-empty homogeneous batch of strings or token IDs.")
    else:
        raise _invalid("input must be a non-empty string or list of strings or token IDs.")

    if len(counts) > MAX_EMBEDDING_BATCH_SIZE:
        raise _invalid(f"input exceeds the gateway batch limit of {MAX_EMBEDDING_BATCH_SIZE} items.")
    if any(count > profile.max_input_tokens for count in counts):
        raise _invalid(f"Each input must contain at most {profile.max_input_tokens} tokens.")
    total = sum(counts)
    if total > profile.max_total_tokens:
        raise _invalid(f"The request exceeds the gateway token limit of {profile.max_total_tokens}.")

    dimensions = profile.native_dimensions if payload.dimensions is None else payload.dimensions
    if not 1 <= dimensions <= profile.native_dimensions:
        raise _invalid("dimensions is outside the supported range for this model.", param="dimensions")
    if payload.user is not None and type(payload.user) is not str:
        raise _invalid("user must be a string.", param="user")
    return ValidatedEmbeddingInput(values, len(counts), total, dimensions)


def _token_id_count(values: list[int], vocabulary_size: int) -> int:
    if any(token < 0 or token >= vocabulary_size for token in values):
        raise _invalid("input contains a token ID outside the tokenizer vocabulary.")
    return len(values)


@lru_cache(maxsize=2)
def _get_tokenizer(name: str):
    try:
        return tiktoken.get_encoding(name)
    except Exception as exc:
        raise OpenAIAPIError(
            503,
            "The embedding tokenizer is unavailable.",
            type="server_error",
            code="embedding_tokenizer_unavailable",
        ) from exc


async def resolve_embedding_model(
    session: AsyncSession,
    principal: ApplicationPrincipal,
    model_id: str,
) -> tuple[Model, Provider, EmbeddingModelProfile]:
    model_service = OpenAIModelService(session)
    cards = await model_service.list_visible(principal)
    visible = {card.id for card in cards.data}
    if model_id not in visible:
        raise OpenAIAPIError(404, "The requested model was not found in this application's model catalog.",
                             code="model_not_found", param="model")
    if model_id in {"auto", "auto:cheap", "auto:balanced", "auto:quality"}:
        raise OpenAIAPIError(400, "Embedding requests require a concrete model ID.",
                             code="unsupported_parameter", param="model")

    candidates = [
        (model, provider)
        for model, provider in await model_service.visible_candidates(principal)
        if model.name == model_id
    ]
    if not candidates:
        # A listed alias is visible, but aliases do not identify a stable vector space.
        if model_id in visible:
            raise OpenAIAPIError(400, "Embedding requests require a concrete model ID.",
                                 code="unsupported_parameter", param="model")
        raise OpenAIAPIError(404, "The requested model was not found in this application's model catalog.",
                             code="model_not_found", param="model")
    if len(candidates) != 1:
        raise OpenAIAPIError(503, "The model name does not resolve to one unique provider model.",
                             type="server_error", code="model_configuration_unavailable", param="model")
    model, provider = candidates[0]
    capabilities = {str(item).strip().lower() for item in (model.capabilities_json or [])}
    if "embedding" not in capabilities:
        raise OpenAIAPIError(400, "The requested model does not support embeddings.",
                             code="unsupported_parameter", param="model")
    if provider.kind != "openai":
        raise OpenAIAPIError(503, "The embedding provider is not supported.",
                             type="server_error", code="embedding_provider_unavailable", param="model")
    profile = EMBEDDING_PROFILES.get(model.name)
    if profile is None:
        raise OpenAIAPIError(503, "The embedding model profile is not supported.",
                             type="server_error", code="embedding_profile_unavailable", param="model")
    if model.input_price_per_million <= 0 or not model.pricing_version:
        raise OpenAIAPIError(503, "The embedding model has no configured input price.",
                             type="server_error", code="embedding_pricing_unavailable", param="model")
    return model, provider, profile
