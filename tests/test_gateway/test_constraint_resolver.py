from src.backend_ai.gateway.validation.constraint_resolver import ConstraintResolver
from src.shared.dtos.gateway import GatewayRequest


def test_vietnamese_json_instruction_with_long_intervening_phrase_is_detected():
    request = GatewayRequest(
        request_id="constraint-vi",
        application_id="app",
        prompt="Trả về kết quả dưới dạng JSON hợp lệ với 2 field: calculation và translation.",
    )

    effective = ConstraintResolver().resolve(request)

    assert effective.constraints.output_format == "json"


def test_english_json_instruction_with_long_intervening_phrase_is_detected():
    request = GatewayRequest(
        request_id="constraint-en",
        application_id="app",
        prompt="Return the completed response as a valid JSON object with two fields.",
    )

    effective = ConstraintResolver().resolve(request)

    assert effective.constraints.output_format == "json"
