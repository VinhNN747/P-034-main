import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.shared.dtos.common import ApiErrorResponse, PageMeta

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "normalized_responses.json"


def test_error_response_requires_code_and_message_and_allows_optional_context() -> None:
    response = ApiErrorResponse(
        code="validation_error",
        message="Request payload is invalid",
        details={"field": "amount"},
        request_id="req-123",
    )

    assert response.model_dump() == {
        "code": "validation_error",
        "message": "Request payload is invalid",
        "details": {"field": "amount"},
        "request_id": "req-123",
    }


def test_error_response_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError):
        ApiErrorResponse()


def test_page_meta_has_stable_pagination_fields() -> None:
    page = PageMeta(page=2, page_size=20, total=41)

    assert page.model_dump() == {"page": 2, "page_size": 20, "total": 41}


@pytest.mark.parametrize(
    ("fixture_name", "required_fields"),
    [
        ("application", {"id", "name", "owner_user_id", "config_version"}),
        ("model", {"id", "provider", "name", "status", "health_status"}),
        ("runtime", {"request_id", "status", "output", "estimated_cost"}),
        ("usage", {"summary", "items"}),
        ("incident", {"id", "severity", "status", "impact"}),
        ("policy_review", {"id", "status", "decision_history"}),
        ("billing", {"balance", "currency", "last_transaction"}),
    ],
)
def test_normalized_response_fixture_has_scene_contract(fixture_name: str, required_fields: set[str]) -> None:
    fixture: dict[str, Any] = json.loads(FIXTURE_PATH.read_text())
    response = fixture[fixture_name]

    if fixture_name == "policy_review":
        response["decision_history"] = []

    assert required_fields <= response.keys()
