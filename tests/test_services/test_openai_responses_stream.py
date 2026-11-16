import json

from openai.types.responses.response_stream_event import ResponseStreamEvent
from pydantic import TypeAdapter

from src.backend_ai.services.openai_responses_projection import project_response
from src.backend_ai.services.openai_responses_stream import encode_response_event, iter_response_events
from tests.test_services.test_openai_responses_projection import _payload, _receipt


def test_buffered_event_sequence_and_final_response_are_sdk_typed():
    response = project_response(_receipt(), _payload(stream=True))
    events = list(iter_response_events(response))

    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [event["sequence_number"] for event in events] == list(range(9))
    assert all(TypeAdapter(ResponseStreamEvent).validate_python(event) for event in events)
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "hello"
    assert events[2]["item"]["id"] == events[3]["item_id"] == events[4]["item_id"]


def test_incomplete_response_ends_with_incomplete_event():
    response = project_response(_receipt(finish_reason="length"), _payload(stream=True))
    events = list(iter_response_events(response))
    assert events[-1]["type"] == "response.incomplete"
    assert events[-1]["response"]["status"] == "incomplete"


def test_sse_framing_has_event_and_finite_json_data():
    event = next(iter_response_events(project_response(_receipt(), _payload())))
    frame = encode_response_event(event)
    assert frame.startswith("event: response.created\ndata: {")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["type"] == "response.created"
    assert "[DONE]" not in frame
