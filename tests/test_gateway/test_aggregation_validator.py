import pytest

from src.backend_ai.gateway.aggregation.validator import AggregationValidator
from src.shared.dtos.gateway import (
    AggregationResult,
    AggregationStatus,
    EffectiveConstraints,
    RequestConstraints,
)


def _constraints(output_format: str) -> EffectiveConstraints:
    return EffectiveConstraints(
        constraints=RequestConstraints(output_format=output_format),
        resolved={},
    )


@pytest.mark.parametrize(
    "answer,valid",
    [
        ('{"calculation": 378, "translation": "AI Gateway is operating normally"}', True),
        ('```json\n{"calculation": 378}\n```', False),
        ("not json", False),
    ],
)
def test_json_output_format_requires_raw_valid_json(answer, valid):
    result = AggregationResult(
        original_request_id="req-json",
        status=AggregationStatus.COMPLETE,
        final_answer=answer,
        strategy="llm",
        reason="test",
    )

    report = AggregationValidator().validate(result, constraints=_constraints("json"))

    assert report.valid is valid
    if not valid:
        assert "INVALID_JSON_FINAL_ANSWER" in report.errors
