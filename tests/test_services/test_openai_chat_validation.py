import pytest

from src.backend_ai.dtos.openai_compat import ChatCompletionRequest
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.openai_chat_validation import validate_chat_support


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("tool_choice", "required", "invalid_request"),
        ("tool_choice", {"type": "function", "function": {"name": "lookup"}}, "invalid_request"),
        ("functions", [{"name": "lookup"}], "unsupported_parameter"),
        ("logprobs", True, "unsupported_parameter"),
        ("future_parameter", 1, "unknown_parameter"),
    ],
)
def test_unimplemented_or_unknown_generation_option_is_not_silently_ignored(field, value, code):
    payload = ChatCompletionRequest.model_validate(
        {"model": "auto", "messages": [{"role": "user", "content": "hello"}], field: value}
    )
    with pytest.raises(OpenAIAPIError) as error:
        validate_chat_support(payload)
    assert error.value.status_code == 400
    assert error.value.param == field
    assert error.value.code == code


def test_documented_empty_tools_and_none_choice_are_noop():
    validate_chat_support(
        ChatCompletionRequest(
            model="auto", messages=[{"role": "user", "content": "hello"}], tools=[], tool_choice="none"
        )
    )


def test_two_completion_limits_are_rejected_as_ambiguous():
    payload = ChatCompletionRequest(
        model="auto", messages=[{"role": "user", "content": "hello"}], max_tokens=24, max_completion_tokens=48
    )
    with pytest.raises(OpenAIAPIError) as error:
        validate_chat_support(payload)
    assert error.value.param == "max_tokens"


@pytest.mark.parametrize("field,value", [("frequency_penalty", 0), ("presence_penalty", 0), ("seed", 42)])
def test_supported_numeric_options_validate_without_dropping_zero(field, value):
    payload = ChatCompletionRequest.model_validate({"model": "auto", "messages": [{"role": "user", "content": "hello"}], field: value})
    validate_chat_support(payload)
    assert getattr(payload, field) == value


@pytest.mark.parametrize('tool_choice', [{'type': 'function', 'function': None}, {'type': 'function', 'function': []}])
def test_malformed_named_tool_choice_is_a_client_error(tool_choice):
    payload = ChatCompletionRequest(model='auto', messages=[{'role': 'user', 'content': 'hi'}],
        tools=[{'type': 'function', 'function': {'name': 'lookup'}}], tool_choice=tool_choice)
    with pytest.raises(OpenAIAPIError) as error:
        validate_chat_support(payload)
    assert error.value.param == 'tool_choice' and error.value.status_code == 400


def test_schema_property_names_are_not_interpreted_as_schema_keywords():
    payload = ChatCompletionRequest(model='auto', messages=[{'role': 'user', 'content': 'hi'}],
        response_format={'type': 'json_schema', 'json_schema': {'name': 'Answer', 'schema': {
            'type': 'object', 'properties': {'not': {'type': 'string'}, 'if': {'type': 'boolean'}},
            'required': ['not', 'if'], 'additionalProperties': False}}})
    validate_chat_support(payload)


def test_tool_result_must_correspond_to_pending_call():
    payload = ChatCompletionRequest(model='auto', messages=[{'role': 'tool', 'tool_call_id': 'foreign', 'content': 'secret'}])
    with pytest.raises(OpenAIAPIError) as error:
        validate_chat_support(payload)
    assert error.value.param == 'messages'


@pytest.mark.parametrize('function', [
    {'name':'lookup','strict':'true'}, {'name':'bad name'},
    {'name':'lookup','future_option':True},
])
def test_invalid_function_metadata_is_rejected_before_execution(function):
    payload = ChatCompletionRequest(model='auto', messages=[{'role':'user','content':'hi'}],
        tools=[{'type':'function','function':function}])
    with pytest.raises(OpenAIAPIError) as error:
        validate_chat_support(payload)
    assert error.value.status_code == 400 and error.value.param == 'tools'


@pytest.mark.parametrize('schema', [
    {'type':'object','properties':{'answer':{'type':'string'}}},
    {'anyOf':[{'type':'object','properties':{},'additionalProperties':False}]},
])
def test_strict_schema_rejects_provider_incompatible_structure(schema):
    payload = ChatCompletionRequest(model='auto', messages=[{'role':'user','content':'hi'}],
        response_format={'type':'json_schema','json_schema':{'name':'Answer','strict':True,'schema':schema}})
    with pytest.raises(OpenAIAPIError) as error:
        validate_chat_support(payload)
    assert error.value.status_code == 400 and error.value.param == 'response_format'


def test_capability_preflight_respects_blocked_models():
    from types import SimpleNamespace

    from src.backend_ai.services.openai_message_execution import preflight_options
    from src.shared.dtos.gateway import RequestConstraints as InternalConstraints
    from src.shared.dtos.runtime_api import RuntimeAPIError
    provider = SimpleNamespace(id='provider',name='provider',kind='openai')
    rows = tuple((None,SimpleNamespace(id=name,name=name,capabilities_json=[]),provider)
                 for name in ('gpt-4o-mini','gpt-5.4-nano'))
    prepared = SimpleNamespace(request_constraints=InternalConstraints(blocked_models={'gpt-4o-mini'}),
        application_policy=InternalConstraints(),routing_snapshot=SimpleNamespace(candidates=rows))
    payload = ChatCompletionRequest(model='auto',temperature=0,messages=[{'role':'user','content':'hi'}])
    with pytest.raises(RuntimeAPIError) as error:
        preflight_options(payload,prepared)
    assert error.value.status_code == 400 and error.value.details['param'] == 'temperature'
