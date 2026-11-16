from src.backend_ai.gateway.understanding.rules import detect_task_types
from src.shared.dtos.gateway import TaskType


def test_api_gateway_rate_limit_question_is_not_classified_as_coding() -> None:
    prompt = (
        "An API Gateway has a limit of 120 requests per minute. "
        "Calculate the total traffic from three services and determine whether it exceeds the limit."
    )

    task_types = detect_task_types(prompt)

    assert TaskType.CODING not in task_types


def test_explicit_api_implementation_request_is_classified_as_coding() -> None:
    task_types = detect_task_types("Build an API endpoint in Python")

    assert TaskType.CODING in task_types
