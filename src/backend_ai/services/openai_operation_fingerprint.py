from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

OpenAISurface = Literal["openai_responses", "openai_embeddings", "openai_chat_completions"]
OPENAI_NORMALIZATION_VERSION = 2


@dataclass(frozen=True)
class OperationFingerprint:
    payload_hash: str
    normalization_version: int


def build_openai_fingerprint(surface: OpenAISurface, payload: dict[str, Any]) -> OperationFingerprint:
    """Hash a fully validated request with defaults already materialized.

    Adapters own field validation and semantic defaulting; this function only
    applies stable canonical JSON encoding and binds the hash to its API
    surface. Keeping those responsibilities separate prevents one endpoint
    from silently changing another endpoint's normalization rules.
    """
    if surface not in {"openai_responses", "openai_embeddings", "openai_chat_completions"}:
        raise ValueError("Unsupported OpenAI operation surface")
    if not isinstance(payload, dict):
        raise TypeError("OpenAI operation payload must be an object")
    encoded = json.dumps(
        {"surface": surface, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return OperationFingerprint(
        payload_hash=hashlib.sha256(encoded).hexdigest(),
        normalization_version=OPENAI_NORMALIZATION_VERSION,
    )
