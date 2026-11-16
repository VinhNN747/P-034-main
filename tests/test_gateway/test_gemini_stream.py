import asyncio
import json

import httpx
import pytest

from src.backend_ai.gateway.providers.base import GenerationOptions
from src.backend_ai.gateway.providers.gemini import GeminiProvider


@pytest.mark.asyncio
async def test_gemini_sse_preserves_deltas_and_reports_final_usage(monkeypatch):
    finished = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"candidates":[{"content":{"parts":[{"text":" hello"}]}}]}\n\n'
            await asyncio.sleep(0.02)
            finished.set()
            yield b'data: {"candidates":[{"content":{"parts":[{"text":" world "},{"text":"hidden","thought":true}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":2,"thoughtsTokenCount":1}}\n\n'

    def handle(request):
        assert request.url.path.endswith(":streamGenerateContent")
        assert request.url.params["alt"] == "sse"
        assert json.loads(request.content)["generationConfig"]["maxOutputTokens"] == 4
        return httpx.Response(200, stream=Body(), headers={"content-type": "text/event-stream"})

    original = httpx.AsyncClient
    monkeypatch.setattr("src.backend_ai.gateway.providers.gemini.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    stream = GeminiProvider(api_key="fixture").stream("prompt", task_id="task", model_id="gemini-2.5-flash",
        timeout_seconds=1, options=GenerationOptions(max_output_tokens=4, hard_completion_token_limit=4))
    first = await anext(stream)
    assert first.text == " hello" and not finished.is_set()
    remaining = [event async for event in stream]
    assert [event.text for event in remaining if event.type == 'text_delta'] == [' world ']
    assert remaining[-1].final.output_tokens == 3
    assert remaining[-1].final.output_evidence.finish_reason == "stop"


def test_gemini_message_sampling_and_schema_payload_preserve_options():
    from src.backend_ai.gateway.providers.base import GenerationOptions
    from src.backend_ai.gateway.providers.gemini import _generation_payload
    options = GenerationOptions(messages=({'role': 'developer', 'content': 'Be precise'},
        {'role': 'user', 'content': 'First'}, {'role': 'assistant', 'content': 'Prior'},
        {'role': 'user', 'content': 'Next'}), temperature=0, top_p=0, seed=42, stop=['STOP'],
        response_format={'type': 'json_schema', 'json_schema': {'name': 'Answer', 'schema': {
            'type': 'object', 'properties': {'answer': {'type': 'string'}}}}})
    payload = _generation_payload('flattened must not be used', 'gemini-2.5-flash', options)
    assert payload['systemInstruction']['parts'] == [{'text': 'Be precise'}]
    assert [item['role'] for item in payload['contents']] == ['user', 'model', 'user']
    assert payload['contents'][-1]['parts'] == [{'text': 'Next'}]
    assert payload['generationConfig']['temperature'] == payload['generationConfig']['topP'] == 0
    assert payload['generationConfig']['seed'] == 42
    assert payload['generationConfig']['stopSequences'] == ['STOP']
    assert payload['generationConfig']['responseMimeType'] == 'application/json'
    assert payload['generationConfig']['responseJsonSchema']['type'] == 'object'


@pytest.mark.asyncio
@pytest.mark.parametrize('tail', ['eof', 'fault'])
async def test_gemini_received_usage_survives_eof_or_later_fault(monkeypatch, tail):
    from src.backend_ai.gateway.providers.base import PermanentProviderError
    payload = {'candidates': [{'content': {'parts': [{'text': 'known'}]}, 'finishReason': 'STOP'}],
        'usageMetadata': {'promptTokenCount': 3, 'candidatesTokenCount': 2}}
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ('data: ' + json.dumps(payload) + ('\n\n' if tail == 'fault' else '')).encode()
            if tail == 'fault':
                yield b'data: invalid-json\n\n'
    original = httpx.AsyncClient
    monkeypatch.setattr('src.backend_ai.gateway.providers.gemini.httpx.AsyncClient',
        lambda **kwargs: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body())), **kwargs))
    events = []
    stream = GeminiProvider(api_key='fixture').stream('prompt', task_id='task', model_id='gemini-2.5-flash', timeout_seconds=1)
    if tail == 'fault':
        with pytest.raises(PermanentProviderError):
            async for event in stream:
                events.append(event)
        assert not any(event.type == 'final' for event in events)
    else:
        events = [event async for event in stream]
        assert events[-1].final.text == 'known'
    measured = [event.final for event in events if event.type == 'usage']
    assert measured and measured[-1].usage_received
    assert (measured[-1].input_tokens, measured[-1].output_tokens) == (3, 2)
