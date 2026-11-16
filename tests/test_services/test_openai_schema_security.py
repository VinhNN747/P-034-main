import io
import json
import urllib.request

import pytest

from src.backend_ai.dtos.openai_compat import ChatCompletionRequest
from src.backend_ai.routers.openai_errors import OpenAIAPIError
from src.backend_ai.services.openai_chat_validation import validate_chat_support
from src.backend_ai.services.openai_schema_service import validate_output_schema


@pytest.mark.parametrize('parameter', ['response_format', 'tools'])
@pytest.mark.parametrize('keyword', ['$dynamicRef', '$recursiveRef'])
def test_external_reference_keywords_are_rejected_before_generation(parameter, keyword):
    schema = {'type':'object', 'properties':{'value':{keyword:'http://127.0.0.1:9999/private-schema'}},
              'required':['value'], 'additionalProperties':False}
    option = ({'response_format':{'type':'json_schema', 'json_schema':{'name':'Answer', 'strict':True, 'schema':schema}}}
              if parameter == 'response_format' else
              {'tools':[{'type':'function','function':{'name':'lookup','strict':True,'parameters':schema}}]})
    payload = ChatCompletionRequest(model='auto', messages=[{'role':'user','content':'Hello'}], **option)
    with pytest.raises(OpenAIAPIError) as error:
        validate_chat_support(payload)
    assert error.value.status_code == 400 and error.value.param == parameter


@pytest.mark.parametrize('keyword', ['$ref', '$dynamicRef'])
def test_final_schema_validator_never_retrieves_external_resources(monkeypatch, keyword):
    contacted = []
    def retrieving(url):
        contacted.append(url)
        return io.BytesIO(json.dumps({'type':'string'}).encode())
    monkeypatch.setattr(urllib.request, 'urlopen', retrieving)
    schema = {'type':'object','properties':{'value':{keyword:'http://127.0.0.1:9999/private-schema'}}}
    result = validate_output_schema('{"value":"hello"}', schema)
    assert contacted == [], 'Final validation must never perform user-directed network requests'
    assert not result.valid
