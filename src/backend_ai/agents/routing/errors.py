from __future__ import annotations

from src.backend_ai.gateway.providers.base import PermanentProviderError, TransientProviderError


class TransientExecutionError(TransientProviderError):
    """Worth retrying against the fixed fallback model."""


class PermanentExecutionError(PermanentProviderError):
    """A fallback would not fix this error and must not mask it."""


def classify_error(error: Exception) -> type[TransientExecutionError] | type[PermanentExecutionError]:
    if isinstance(error, (TransientProviderError, TransientExecutionError)):
        return TransientExecutionError
    if getattr(error, "transient", False):
        return TransientExecutionError

    # langchain-core ModelError is the common base used by current TypeSafe
    # and model middleware integrations. Keep the import lazy so offline
    # startup does not require an external SDK to classify local errors.
    try:
        from langchain_core.exceptions import ModelError

        model_error_type = ModelError
    except ImportError:
        model_error_type = None  # type: ignore[assignment]
    if model_error_type is not None and isinstance(error, model_error_type) and getattr(error, "is_retryable", False):
        return TransientExecutionError

    if isinstance(error, PermanentProviderError):
        return PermanentExecutionError
    return PermanentExecutionError
