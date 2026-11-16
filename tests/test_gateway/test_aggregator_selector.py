import pytest

from src.backend_ai.gateway.aggregation.selector import resolve_aggregator


def test_auto_uses_no_aggregator_for_one_task():
    selection = resolve_aggregator(
        "auto",
        1,
        {"none": object(), "deterministic": object(), "llm": object()},
    )

    assert selection.requested == "auto"
    assert selection.selected == "none"
    assert selection.automatic is True


def test_auto_uses_llm_for_multiple_tasks_when_available():
    selection = resolve_aggregator(
        "auto",
        2,
        {"none": object(), "deterministic": object(), "llm": object()},
    )

    assert selection.selected == "llm"
    assert selection.reason == "multiple_tasks_llm_available"


def test_auto_uses_deterministic_for_multiple_tasks_without_llm():
    selection = resolve_aggregator(
        "auto",
        2,
        {"none": object(), "deterministic": object()},
    )

    assert selection.selected == "deterministic"
    assert selection.reason == "multiple_tasks_llm_unavailable"


def test_explicit_none_is_preserved_for_multiple_tasks():
    selection = resolve_aggregator("none", 2, {"none": object(), "deterministic": object()})

    assert selection.requested == "none"
    assert selection.selected == "none"
    assert selection.automatic is False


def test_selector_rejects_unknown_aggregator():
    with pytest.raises(ValueError, match="Unknown aggregator 'missing'"):
        resolve_aggregator("missing", 1, {"none": object()})
