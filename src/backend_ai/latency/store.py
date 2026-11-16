from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping
from typing import Any

from src.backend_ai.latency.contracts import LatencyArtifact, thaw

logger = logging.getLogger(__name__)
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


def artifact_to_payload(artifact: LatencyArtifact) -> dict[str, Any]:
    return {
        "schema_version": artifact.schema_version,
        "version": artifact.version,
        "feature_names": list(artifact.feature_names),
        "coefficients": list(artifact.coefficients),
        "intercept": artifact.intercept,
        "numeric_means": thaw(artifact.numeric_means),
        "numeric_scales": thaw(artifact.numeric_scales),
        "categories": {key: list(value) for key, value in artifact.categories.items()},
        "support": thaw(artifact.support),
        "validation_report": thaw(artifact.validation_report),
        "checksum": artifact.checksum,
    }


def artifact_checksum(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "checksum"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(canonical).hexdigest()


def sign_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body.pop("checksum", None)
    body["checksum"] = artifact_checksum(body)
    return body


def validate_artifact(payload: Mapping[str, Any]) -> LatencyArtifact:
    if not isinstance(payload, Mapping):
        raise ValueError("artifact must be a JSON object")
    try:
        encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact contains invalid JSON values") from exc
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact exceeds 2 MiB")
    if payload.get("schema_version") != "latency-v1":
        raise ValueError("unsupported artifact schema")
    if not isinstance(payload.get("version"), str) or not payload["version"]:
        raise ValueError("artifact version is required")
    feature_names = payload.get("feature_names")
    coefficients = payload.get("coefficients")
    if not isinstance(feature_names, list) or not isinstance(coefficients, list):
        raise ValueError("artifact feature names and coefficients must be arrays")
    if len(feature_names) != len(coefficients) or len(feature_names) > 256:
        raise ValueError("artifact feature dimension is invalid")
    if not all(isinstance(name, str) and name for name in feature_names) or len(set(feature_names)) != len(feature_names):
        raise ValueError("artifact feature names must be unique non-empty strings")
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in coefficients):
        raise ValueError("artifact coefficients must be finite")
    intercept = payload.get("intercept")
    if not isinstance(intercept, (int, float)) or not math.isfinite(intercept):
        raise ValueError("artifact intercept must be finite")
    means = payload.get("numeric_means", {})
    scales = payload.get("numeric_scales", {})
    if not isinstance(means, Mapping) or not isinstance(scales, Mapping):
        raise ValueError("artifact scaler must be an object")
    if set(means) != set(scales):
        raise ValueError("artifact scaler dimensions differ")
    for name, value in means.items():
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"invalid scaler mean: {name}")
        scale = scales[name]
        if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
            raise ValueError(f"invalid scaler scale: {name}")
    categories = payload.get("categories", {})
    support = payload.get("support", {})
    validation_report = payload.get("validation_report", {})
    if not isinstance(categories, Mapping) or not isinstance(support, Mapping) or not isinstance(validation_report, Mapping):
        raise ValueError("artifact support, categories and report must be objects")
    support_model_groups = {
        profile.get("model_key_hash") for profile in support.values()
        if isinstance(profile, Mapping) and profile.get("model_key_hash") is not None
    }
    if len(support_model_groups) > 128:
        raise ValueError("artifact exceeds model group limit")
    if not all(isinstance(key, str) and isinstance(values, list) and all(isinstance(value, str) for value in values)
               for key, values in categories.items()):
        raise ValueError("artifact categories are invalid")
    if sum(len(values) for values in categories.values()) > 256:
        raise ValueError("artifact category dimension exceeds feature limit")
    checksum = payload.get("checksum")
    if not isinstance(checksum, str) or checksum != artifact_checksum(payload):
        raise ValueError("artifact checksum mismatch")
    return LatencyArtifact(
        schema_version="latency-v1",
        version=payload["version"],
        feature_names=tuple(feature_names),
        coefficients=tuple(float(value) for value in coefficients),
        intercept=float(intercept),
        numeric_means=means,
        numeric_scales=scales,
        categories={key: tuple(values) for key, values in categories.items()},
        support=support,
        validation_report=validation_report,
        checksum=checksum,
    )


def load_artifact_json(contents: str | bytes) -> LatencyArtifact:
    try:
        payload = json.loads(contents)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise ValueError("artifact JSON is invalid or truncated") from exc
    return validate_artifact(payload)


class LatencyModelStore:
    """Process-local immutable active artifact snapshot, refreshed from the DB registry."""

    def __init__(self) -> None:
        self._artifact: LatencyArtifact | None = None
        self._generation = 0

    def snapshot(self) -> LatencyArtifact | None:
        return self._artifact

    @property
    def generation(self) -> int:
        return self._generation

    async def refresh(self, repository) -> None:
        generation, artifact = await repository.read_active()
        if artifact is None:
            self._artifact = None
            self._generation = generation
            return
        try:
            validated = validate_artifact(artifact_to_payload(artifact))
        except (TypeError, ValueError) as exc:
            logger.warning("active latency artifact rejected: %s", type(exc).__name__)
            return
        self._artifact = validated
        self._generation = generation
