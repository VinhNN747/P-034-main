"""eval/difficulty: dataset schema, labels, leakage-safe splits and metrics (pure Python, no sklearn)."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import replace

import pytest
from eval.difficulty.build_dataset import _bench_ok, _label
from eval.difficulty.metrics import (
    average_precision,
    bootstrap_difference,
    brier,
    confusion_at,
    expected_calibration_error,
    roc_auc,
    simulate,
    threshold_for_rate,
)
from eval.difficulty.schema import (
    DatasetError,
    DatasetRow,
    detect_language,
    load_dataset,
    prompt_sha,
    require_two_classes,
    strip_benchmark_template,
    validate_row,
)
from eval.difficulty.splits import assign_groups, check_no_leakage, split_groups


def make_row(i: int, prompt: str | None = None, label: str = "easy", dataset: str = "gsm8k") -> DatasetRow:
    prompt = prompt or f"Question number {i}: how many apples are left after step {i * 7}?"
    return DatasetRow(
        sample_id=f"s{i}",
        prompt=prompt,
        label=label,
        source="test",
        dataset=dataset,
        language="en",
        small_model="small",
        upper_models=("big",),
        prompt_sha=prompt_sha(prompt),
    )


def as_dict(row: DatasetRow) -> dict:
    return json.loads(row.to_json())


# --- schema -----------------------------------------------------------------------------------


def test_round_trip_and_target(tmp_path):
    rows = [make_row(1), make_row(2, label="hard"), make_row(3, label="unsolvable")]
    path = tmp_path / "d.jsonl"
    path.write_text("".join(r.to_json() + "\n" for r in rows), encoding="utf-8")
    loaded = load_dataset(path)
    assert loaded == rows
    assert [r.target for r in loaded] == [0, 1, None]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"prompt": None}, "missing fields: prompt"),
        ({"label": "medium"}, "label 'medium'"),
        ({"language": "fr"}, "language 'fr'"),
        ({"prompt_sha": "0" * 16}, "prompt_sha does not match"),
        ({"upper_models": "big"}, "upper_models must be a list"),
        ({"dataset": 3}, "dataset must be a string"),
    ],
)
def test_validate_row_rejects(change, message):
    data = as_dict(make_row(1)) | change
    with pytest.raises(DatasetError, match=message):
        validate_row(data)


def test_load_dataset_names_the_bad_line(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text(make_row(1).to_json() + "\n{not json\n", encoding="utf-8")
    with pytest.raises(DatasetError, match=r"d.jsonl:2: not JSON"):
        load_dataset(path)


def test_load_dataset_rejects_duplicate_ids_and_empty_files(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text(make_row(1).to_json() + "\n" + make_row(1).to_json() + "\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="duplicate sample_id s1"):
        load_dataset(path)
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="no rows"):
        load_dataset(path)


def test_require_two_classes():
    rows = [make_row(i) for i in range(30)]
    with pytest.raises(DatasetError, match="0 hard"):
        require_two_classes(rows, "train")
    rows += [make_row(100 + i, label="hard") for i in range(3)]
    with pytest.raises(DatasetError, match="3 hard"):
        require_two_classes(rows, "train")
    require_two_classes(rows, "train", minimum=3)


# --- text helpers -----------------------------------------------------------------------------


def test_strip_routerarena_template_keeps_question_and_options():
    raw = (
        "Please read the following multiple-choice questions and provide the most likely correct answer "
        "based on the options given.\n\nContext: None\n\nQuestion: How fast is USB 3.1 Gen 2?\n\n"
        "Options: \nA. 8 Gb/s\nB. 10 Gb/s\n\n\nProvide the correct letter choice in \\boxed{X}, where X is "
        "the correct letter choice. Keep the explanation or feedback within 3 sentences."
    )
    assert strip_benchmark_template(raw) == "How fast is USB 3.1 Gen 2?\n\nOptions: \nA. 8 Gb/s\nB. 10 Gb/s"


def test_strip_answer_bench_template():
    raw = "Giải bài toán sau (đề tiếng Anh).\n\nFind $x$ if $2x=6$.\nĐưa đáp số cuối cùng trong \\boxed{}."
    assert strip_benchmark_template(raw) == "Find $x$ if $2x=6$."
    choice = "Thủ đô của Việt Nam là\nA. Hà Nội\nB. Huế\nChọn một đáp án đúng."
    assert strip_benchmark_template(choice) == "Thủ đô của Việt Nam là\nA. Hà Nội\nB. Huế"


def test_strip_never_returns_empty():
    assert strip_benchmark_template("Provide the correct letter") == "Provide the correct letter"


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Mary còn lại bao nhiêu chậu cây?", "vi"),
        ("How many apples are left?", "en"),
        ("北京是中国的首都吗", "zh"),
        ("12345 + 678", "other"),
    ],
)
def test_detect_language(text, language):
    assert detect_language(text) == language


# --- labels -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("small", "upper", "label"),
    [
        (True, [], "easy"),
        (True, [False], "easy"),
        (False, [False, True], "hard"),
        (False, [False, False], "unsolvable"),
        (False, [None, False], "unknown"),
        (None, [True], None),
    ],
)
def test_label(small, upper, label):
    assert _label(small, upper) == label


def test_bench_ok_uses_only_the_exact_check():
    assert _bench_ok({"status": "ok", "exact": True}) is True
    assert _bench_ok({"status": "ok", "exact": False}) is False
    assert _bench_ok({"status": "ok", "exact": None, "judge": {"correct": 1.0}}) is None  # judge is ignored
    assert _bench_ok({"status": "error"}) is None
    assert _bench_ok({"status": "empty"}) is False
    assert _bench_ok(None) is None


# --- groups and splits ------------------------------------------------------------------------


def test_assign_groups_joins_exact_and_near_duplicates():
    base = "A train leaves the station at nine and travels sixty miles per hour toward the city center"
    rows = [make_row(1, base), make_row(2, base), make_row(3, base + " today"), make_row(4)]
    grouped, merged = assign_groups(rows)
    groups = [r.group_id for r in grouped]
    assert groups[0] == groups[1] == groups[2] != groups[3]
    assert merged == 2


def test_split_groups_keeps_groups_together_and_hits_fractions():
    rows = [make_row(i, label="hard" if i % 4 == 0 else "easy", dataset=f"d{i % 3}") for i in range(200)]
    rows += [replace(make_row(500 + i), prompt=rows[i].prompt, prompt_sha=rows[i].prompt_sha) for i in range(20)]
    rows, _ = assign_groups(rows)
    splits = split_groups(rows)
    check_no_leakage(rows, splits)
    counts = Counter(splits.values())
    assert abs(counts["train"] / len(rows) - 0.6) < 0.05
    assert abs(counts["test"] / len(rows) - 0.2) < 0.05
    assert split_groups(rows) == splits  # deterministic
    for name in ("train", "val", "test"):
        assert {r.label for r in rows if splits[r.sample_id] == name} == {"easy", "hard"}


def test_check_no_leakage_catches_a_duplicate_across_splits():
    a, b = make_row(1, "same prompt text here"), make_row(2, "same prompt text here")
    a, b = replace(a, group_id="g1"), replace(b, group_id="g2")  # grouping was skipped
    with pytest.raises(DatasetError, match="appear in two splits"):
        check_no_leakage([a, b], {"s1": "train", "s2": "test"})
    with pytest.raises(DatasetError, match="has no split"):
        check_no_leakage([a], {})


# --- metrics ----------------------------------------------------------------------------------


def test_auc_and_average_precision():
    y = [0, 0, 1, 1]
    assert roc_auc(y, [0.1, 0.4, 0.35, 0.8]) == pytest.approx(0.75)
    assert roc_auc(y, [0.5, 0.5, 0.5, 0.5]) == pytest.approx(0.5)
    assert average_precision(y, [0.1, 0.4, 0.35, 0.8]) == pytest.approx((1 + 2 / 3) / 2)
    assert math.isnan(roc_auc([1, 1], [0.2, 0.3]))  # one class only


def test_probability_metrics():
    y, p = [0, 1, 1, 0], [0.0, 1.0, 1.0, 0.0]
    assert brier(y, p) == 0
    assert expected_calibration_error(y, p) == 0
    assert expected_calibration_error([0, 1], [0.9, 0.1]) == pytest.approx(0.9)
    c = confusion_at([0, 1, 1, 0], [0.6, 0.7, 0.2, 0.1], 0.5)
    assert (c.tp, c.fp, c.tn, c.fn) == (1, 1, 1, 1)


@pytest.mark.parametrize("scores", [[0.1, math.nan], [0.1, math.inf]])
def test_metrics_reject_invalid_scores(scores):
    with pytest.raises(ValueError, match="finite"):
        roc_auc([0, 1], scores)


def test_calibration_rejects_non_probabilities():
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        expected_calibration_error([0, 1], [0.2, 1.5])


def test_bootstrap_difference_of_a_clearly_better_scorer():
    y = [0] * 50 + [1] * 50
    good = [i / 100 for i in range(100)]
    noise = [((i * 37) % 100) / 100 for i in range(100)]
    estimate, low, high = bootstrap_difference(y, good, noise, rounds=200)
    assert estimate > 0 and low > 0 and high >= estimate


def test_simulate_and_threshold():
    labels = ["easy", "hard", "unsolvable", "hard", "unknown"]
    scores = [0.1, 0.9, 0.8, 0.2, 0.99]
    res = simulate(labels, scores, threshold=0.5, upgrade_cost=4)
    assert (res.rows, res.upgraded, res.correct) == (4, 2, 2)
    assert (res.hard_upgraded, res.hard_total, res.wasted_on_unsolvable) == (1, 2, 1)
    assert res.relative_cost == pytest.approx((2 + 2 * 4) / 4)
    assert threshold_for_rate([0.1, 0.2, 0.3, 0.4], 0.5) == 0.3
    assert threshold_for_rate([0.1], 0) == math.inf
    with pytest.raises(ValueError, match="easy, hard"):
        simulate(["medium"], [0.1], 0.5)
