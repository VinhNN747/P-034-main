import json
import math
import subprocess
import sys

import pytest

from src.backend_ai.latency.contracts import LatencyArtifact
from src.backend_ai.latency.store import (
    LatencyModelStore,
    load_artifact_json,
    sign_artifact,
    validate_artifact,
)


def _payload():
    return sign_artifact(
        {
            "schema_version": "latency-v1",
            "version": "v1",
            "feature_names": ["numeric:x"],
            "coefficients": [0.25],
            "intercept": 2.0,
            "numeric_means": {"x": 1.0},
            "numeric_scales": {"x": 2.0},
            "categories": {},
            "support": {"profile": {"train_count": 60, "validation_count": 25}},
            "validation_report": {"accepted": True},
        }
    )


def test_invalid_artifact_is_rejected_for_shape_nonfinite_checksum_or_truncation():
    payload = _payload()
    assert validate_artifact(payload).version == "v1"
    wrong_shape = {**payload, "coefficients": [0.25, 0.5]}
    nonfinite = {**payload, "intercept": math.nan}
    bad_checksum = {**payload, "intercept": 3.0}

    for invalid in (wrong_shape, nonfinite, bad_checksum):
        with pytest.raises(ValueError):
            validate_artifact(invalid)
    with pytest.raises(ValueError):
        load_artifact_json('{"schema_version":"latency-v1"')


@pytest.mark.asyncio
async def test_store_refresh_replaces_immutable_snapshot_and_keeps_old_on_bad_artifact():
    artifact = validate_artifact(_payload())
    store = LatencyModelStore()

    class Repository:
        async def read_active(self):
            return 1, artifact

    await store.refresh(Repository())
    assert store.snapshot() == artifact
    assert store.generation == 1

    class BrokenRepository:
        async def read_active(self):
            return 2, LatencyArtifact("latency-v1", "bad", (), (), 1.0)

    await store.refresh(BrokenRepository())
    assert store.snapshot() == artifact
    assert store.generation == 1


def test_artifact_json_round_trip_uses_checksum_and_no_numpy_at_import():
    artifact = validate_artifact(_payload())
    json_text = json.dumps(_payload(), separators=(",", ":"))
    assert load_artifact_json(json_text) == artifact
    assert len(artifact.checksum) == 64


def test_management_and_artifact_imports_do_not_load_numpy_or_trainer():
    code = (
        "import sys; import src.shared.models; import src.backend_ai.latency.store; "
        "assert 'numpy' not in sys.modules; "
        "assert 'src.backend_ai.latency.trainer' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
