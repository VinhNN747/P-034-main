import json
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from src.backend_ai.latency.contracts import LatencyConfig, LatencyFeatures, LatencyObservation, ModelKey
from src.backend_ai.latency.dataset import build_dataset, read_dataset, write_dataset

BASE = datetime(2026, 10, 14, tzinfo=UTC)
KEY = ModelKey("model", "provider", "upstream", "fingerprint", "openai")


def _observation(
    attempt: str,
    request: str,
    index: int,
    task: int,
    *,
    finish_delay: int = 1,
    status: str = "success",
    source: str = "openai_chat",
    execution_mode: str = "live",
    training_eligible: bool = True,
    latency_ms: float = 100.0,
) -> LatencyObservation:
    dispatched = BASE + timedelta(seconds=index)
    finished = dispatched + timedelta(seconds=finish_delay)
    features = LatencyFeatures(KEY, "profile", dispatched, {"input_proxy": index + 1, "output_budget": 512})
    return LatencyObservation(
        attempt, request, f"task-{task}", "app", features, source, "observe", dispatched, status,
        finished, latency_ms, execution_mode=execution_mode, training_eligible=training_eligible,
    )


def test_dataset_excludes_censored_probe_offline_and_slow_stream_samples():
    rows = []
    for index in range(10):
        rows.extend(
            [
                _observation(f"{index}-0", f"request-{index}", index, 0, latency_ms=100 + index),
                _observation(f"{index}-1", f"request-{index}", index, 1, latency_ms=5000 + index),
            ]
        )
    rows.extend(
        [
            _observation("timeout", "timeout-request", 20, 0, status="timeout"),
            _observation("probe", "probe-request", 21, 0, source="probe"),
            _observation("offline", "offline-request", 22, 0, execution_mode="offline"),
            _observation("paused", "paused-request", 23, 0, training_eligible=False),
        ]
    )

    dataset = build_dataset(rows, cutoff=BASE + timedelta(seconds=30), config=LatencyConfig())
    train_requests = {row.request_id for row in dataset.train_rows}
    validation_requests = {row.request_id for row in dataset.validation_rows}
    all_rows = dataset.train_rows + dataset.validation_rows

    assert train_requests.isdisjoint(validation_requests)
    assert not {"timeout", "probe", "offline", "paused"}.intersection(row.attempt_id for row in all_rows)
    assert any(row.attempt_id == "0-1" and row.provider_total_ms >= 5000 for row in all_rows)
    assert len(train_requests) > len(validation_requests)


def test_dataset_excludes_train_outcomes_that_finish_after_validation_cutoff():
    rows = [
        _observation(f"a-{index}", f"request-{index}", index, 0, finish_delay=1) for index in range(10)
    ]
    rows.append(_observation("straddling", "request-7", 7, 1, finish_delay=20))

    dataset = build_dataset(rows, cutoff=BASE + timedelta(seconds=30), config=LatencyConfig())

    assert all(row.attempt_id != "straddling" for row in dataset.train_rows + dataset.validation_rows)
    assert {row.request_id for row in dataset.train_rows}.isdisjoint(
        {row.request_id for row in dataset.validation_rows}
    )


def test_dataset_caps_rows_groups_deterministically_and_writes_private_json(tmp_path):
    rows = [
        _observation(f"a-{index}", f"request-{index}", index, 0) for index in range(10)
    ]
    config = LatencyConfig(max_training_rows=6, max_model_groups=1)
    first = build_dataset(rows, cutoff=BASE + timedelta(seconds=30), config=config)
    second = build_dataset(list(reversed(rows)), cutoff=BASE + timedelta(seconds=30), config=config)
    path = tmp_path / "snapshot.json"
    write_dataset(first, path)

    assert first.digest == second.digest
    assert len(first.train_rows) + len(first.validation_rows) <= 6
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text())
    assert "prompt" not in path.read_text().lower()
    assert payload["digest"] == first.digest
    assert read_dataset(path) == first


def test_dataset_keeps_only_best_supported_model_groups_deterministically():
    common = [
        _observation(f"common-{index}", f"common-request-{index}", index, 0) for index in range(8)
    ]
    other_key = ModelKey("other", "provider", "upstream-other", "fingerprint", "openai")
    other = []
    for index in range(2):
        observation = _observation(f"other-{index}", f"other-request-{index}", 20 + index, 0)
        other.append(
            replace(
                observation,
                features=LatencyFeatures(other_key, "other-profile", observation.dispatched_at, observation.features.values),
            )
        )

    dataset = build_dataset(common + other, cutoff=BASE + timedelta(seconds=30), config=LatencyConfig(max_model_groups=1))

    assert {row.features.model_key.digest() for row in dataset.train_rows + dataset.validation_rows} == {KEY.digest()}
