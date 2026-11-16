import json
from pathlib import Path

from src.shared.dtos.runtime_api import RuntimeErrorEnvelope, RuntimeRequestInput, RuntimeRequestResponse


def _fixture() -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / "runtime_api_v1.json").read_text())


def test_runtime_v1_contract_fixture_matches_backend_dtos():
    fixture = _fixture()
    request = RuntimeRequestInput.model_validate(fixture["request"])
    response = RuntimeRequestResponse.model_validate(fixture["response"])
    error = RuntimeErrorEnvelope.model_validate(fixture["error"])
    assert request.prompt == "Summarize this text."
    assert response.status == "completed"
    assert response.usage and response.usage.pipeline.total_tokens == 17
    assert error.error.code == "REQUEST_IN_PROGRESS"
