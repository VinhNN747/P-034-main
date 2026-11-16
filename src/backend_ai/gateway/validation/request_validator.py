from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from src.backend_ai.gateway.core.config import GatewayConfig
from src.shared.dtos.gateway import (
    GatewayRequest,
    IssueSeverity,
    ValidationIssue,
    ValidationResult,
)

_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|authorization|access[_-]?token|client[_-]?secret|password)", re.I)
_SENSITIVE_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~-]{12,}|sk-[A-Za-z0-9_-]{12,}|(?:ak|off)_[A-Za-z0-9_-]{32,})",
    re.I,
)


def estimate_tokens(text: str) -> int:
    """Conservative provider-independent estimate; exact counting belongs to provider adapters."""
    if not text:
        return 0
    word_based = math.ceil(len(text.split()) * 1.35)
    char_based = math.ceil(len(text) / 4)
    return max(1, word_based, char_based)


def _contains_credentials(value: Any, path: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if _SENSITIVE_KEY.search(str(key)):
                return child_path
            found = _contains_credentials(nested, child_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found = _contains_credentials(nested, f"{path}[{index}]")
            if found:
                return found
    elif isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        return path
    return None


class RequestValidator:
    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()

    def validate(self, request: GatewayRequest) -> ValidationResult:
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        prompt = request.prompt_text()
        context_text = json.dumps(request.context, ensure_ascii=False, default=str)
        estimated_tokens = estimate_tokens(f"{prompt}\n{context_text}")

        def error(code: str, field: str, message: str) -> None:
            errors.append(ValidationIssue(code=code, field=field, message=message, severity=IssueSeverity.ERROR))

        def warning(code: str, field: str, message: str) -> None:
            warnings.append(ValidationIssue(code=code, field=field, message=message, severity=IssueSeverity.WARNING))

        attachment_count = sum(len(message.media_parts()) for message in request.messages or [])
        if not prompt.strip() and not attachment_count:
            error("EMPTY_PROMPT", "prompt", "Prompt/messages must contain non-whitespace content")
        if attachment_count > self.config.max_attachments:
            error(
                "TOO_MANY_ATTACHMENTS",
                "messages",
                f"Request has {attachment_count} image/file parts; the limit is {self.config.max_attachments}",
            )
        if len(prompt) > self.config.max_prompt_chars or estimated_tokens > self.config.max_input_tokens:
            error(
                "PROMPT_TOO_LARGE",
                "prompt",
                f"Request exceeds {self.config.max_prompt_chars} characters or "
                f"{self.config.max_input_tokens} estimated tokens",
            )

        constraints = request.constraints
        if constraints.max_cost_usd is not None and constraints.max_cost_usd <= 0:
            error("INVALID_CONSTRAINT", "constraints.max_cost_usd", "max_cost_usd must be positive")
        if constraints.max_latency_ms is not None and constraints.max_latency_ms <= 0:
            error("INVALID_CONSTRAINT", "constraints.max_latency_ms", "max_latency_ms must be positive")
        if constraints.max_output_tokens is not None and constraints.max_output_tokens <= 0:
            error("INVALID_CONSTRAINT", "constraints.max_output_tokens", "max_output_tokens must be positive")
        if constraints.output_format.lower() not in self.config.supported_output_formats:
            error(
                "UNSUPPORTED_OUTPUT_FORMAT",
                "constraints.output_format",
                f"Supported formats: {sorted(self.config.supported_output_formats)}",
            )

        for kind, allowed, blocked in (
            ("model", constraints.allowed_models, constraints.blocked_models),
            ("provider", constraints.allowed_providers, constraints.blocked_providers),
        ):
            if allowed is not None and not allowed:
                error("NO_ELIGIBLE_MODEL", f"constraints.allowed_{kind}s", f"Allowed {kind} set is empty")
            overlap = (allowed or set()) & blocked
            if overlap:
                error(
                    "CONSTRAINT_CONFLICT",
                    f"constraints.allowed_{kind}s",
                    f"The same {kind}(s) are allowed and blocked: {sorted(overlap)}",
                )
            if allowed is not None and not (allowed - blocked):
                error("NO_ELIGIBLE_MODEL", f"constraints.allowed_{kind}s", f"No eligible {kind} remains")

        sensitive_path = _contains_credentials(
            {"prompt": prompt, "context": request.context, "metadata": request.metadata}
        )
        if sensitive_path:
            error(
                "CREDENTIAL_EXPOSURE",
                sensitive_path,
                "Credentials or authorization data must not be placed in the request payload",
            )

        if estimated_tokens > self.config.max_input_tokens * 0.8:
            warning("PROMPT_NEAR_LIMIT", "prompt", "Estimated input size is above 80% of the configured limit")

        return ValidationResult(
            accepted=not errors,
            errors=errors,
            warnings=warnings,
            estimated_input_tokens=estimated_tokens,
        )
