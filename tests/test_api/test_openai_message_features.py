import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from src.backend_ai.gateway.providers.offline import OfflineProvider
from tests.test_api.test_openai_compat import openai_env as openai_env


@pytest.mark.asyncio
async def test_sdk_message_workflow_keeps_dev_tier_signals_affinity_and_shadow(openai_env, monkeypatch):
    from src.backend_ai.gateway.routing.session_affinity import SessionAffinity, SessionAffinityStore
    from src.backend_ai.gateway.routing.tier_policy import TierPolicy
    from src.backend_ai.services import gateway_execution_service as execution
    affinity=SessionAffinity(SessionAffinityStore(),'merge-fixture')
    monkeypatch.setattr(execution.SessionAffinity,'for_request',lambda *args,**kwargs: affinity)
    monkeypatch.setattr(execution,'build_shadow_tier_policy',lambda settings: TierPolicy())
    captured=[]
    original=execution.GatewayExecutionService.run_public
    async def run(self,*args,**kwargs):
        result=await original(self,*args,**kwargs)
        captured.append(result)
        return result
    monkeypatch.setattr(execution.GatewayExecutionService,'run_public',run)
    headers={'Authorization':'Bearer '+openai_env['key'],'Idempotency-Key':'merge-message-routing'}
    body={'model':'auto','messages':[{'role':'system','content':'Answer using this conversation.'},
        {'role':'user','content':'Hello'}],'temperature':0,'max_completion_tokens':48}
    first=await openai_env['client'].post('/v1/chat/completions',json=body,headers=headers)
    replay=await openai_env['client'].post('/v1/chat/completions',json=body,headers=headers)
    assert first.status_code==replay.status_code==200,first.text
    assert first.json()==replay.json() and len(captured)==1
    task=captured[0].decomposition.subtasks[0]
    assert task.routing_signals is not None and task.routing_signals.context_chars>0
    result=captured[0].execution_results[0]
    assert result.route_reason and result.route_shadow and 'actual_tier' in result.route_shadow
    assert affinity.task_id==task.task_id and affinity.previous_model()==result.executed_model


@pytest.mark.asyncio
async def test_sampling_zero_and_tool_history_are_preserved_before_provider(openai_env, monkeypatch):
    captured = []
    original = OfflineProvider.generate

    async def generate(self, prompt, **kwargs):
        captured.append(kwargs.get("options"))
        return await original(self, prompt, **kwargs)

    monkeypatch.setattr(OfflineProvider, "generate", generate)
    response = await openai_env["client"].post("/v1/chat/completions", headers={
        "Authorization": "Bearer " + openai_env["key"], "Idempotency-Key": "message-fidelity"}, json={
        "model": "auto", "temperature": 0, "top_p": 0, "messages": [
            {"role": "user", "content": "look up Paris"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "lookup", "arguments": '{"city":"Paris"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "sunny"}]})
    assert response.status_code == 200, response.text
    options = captured[-1]
    assert options.temperature == options.top_p == 0
    assert options.messages[1]["content"] is None
    assert options.messages[1]["tool_calls"][0]["id"] == "call-1"
    assert options.messages[2]["tool_call_id"] == "call-1"


@pytest.mark.asyncio
async def test_sdk_parse_uses_native_schema_and_validates_final_output(openai_env, monkeypatch):
    import httpx

    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.shared.dtos.gateway import OutputEvidence

    class Answer(BaseModel):
        answer: str

    captured = []

    async def generate(self, prompt, *, model_id, options=None, **kwargs):
        captured.append(options)
        return ProviderResponse(text='{"answer":"ok"}', provider="offline", model_id=model_id,
            usage_received=True, input_tokens=3, output_tokens=2, output_evidence=OutputEvidence(finish_reason="stop"))

    monkeypatch.setattr(OfflineProvider, "generate", generate)
    transport = httpx.AsyncClient(transport=httpx.ASGITransport(app=__import__("tests.service_app", fromlist=["app"]).app),
        base_url="http://test")
    async with AsyncOpenAI(base_url="http://test/v1", api_key=openai_env["key"], http_client=transport) as client:
        response = await client.chat.completions.parse(model="auto", messages=[{"role": "user", "content": "json"}],
            response_format=Answer, extra_headers={"Idempotency-Key": "native-schema"})
    assert response.choices[0].message.parsed.answer == "ok"
    assert captured[-1].response_format["type"] == "json_schema"
    assert captured[-1].response_format["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_function_call_round_trip_in_chat_and_responses_sdk(openai_env, monkeypatch):
    import httpx

    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.shared.dtos.gateway import OutputEvidence
    from tests.service_app import app

    captured = []
    async def generate(self, prompt, *, model_id, options, **kwargs):
        captured.append(options)
        called = options.messages[-1]['role'] == 'tool'
        message = {'role': 'assistant', 'content': 'sunny' if called else None}
        if not called:
            message['tool_calls'] = [{'id': 'call_weather', 'type': 'function',
                'function': {'name': 'weather', 'arguments': '{"city":"Paris"}'}}]
        finish = 'stop' if called else 'tool_calls'
        return ProviderResponse(text=message['content'] or '', provider='offline', model_id=model_id,
            input_tokens=3, output_tokens=2, usage_received=True, output_evidence=OutputEvidence(finish_reason=finish),
            metadata={'openai_choices': [{'index': 0, 'message': message, 'finish_reason': finish}]})
    monkeypatch.setattr(OfflineProvider, 'generate', generate)
    tool = {'type': 'function', 'function': {'name': 'weather', 'parameters': {
        'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city'], 'additionalProperties': False}}}
    transport = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    async with AsyncOpenAI(base_url='http://test/v1', api_key=openai_env['key'], http_client=transport,
                           _strict_response_validation=True, max_retries=0) as client:
        chat = await client.chat.completions.create(model='auto', messages=[{'role': 'user', 'content': 'Weather?'}],
            tools=[tool], tool_choice='required', extra_headers={'Idempotency-Key': 'chat-call'})
        call = chat.choices[0].message.tool_calls[0]
        assert call.id == 'call_weather' and chat.choices[0].finish_reason == 'tool_calls'
        followup = await client.chat.completions.create(model='auto', messages=[{'role': 'user', 'content': 'Weather?'},
            chat.choices[0].message.model_dump(exclude_none=True), {'role': 'tool', 'tool_call_id': call.id, 'content': 'sunny'}],
            tools=[tool], extra_headers={'Idempotency-Key': 'chat-result'})
        assert followup.choices[0].message.content == 'sunny'
        response = await client.responses.create(model='auto', input='Weather?', tools=[{'type': 'function', **tool['function']}],
            tool_choice='required', extra_headers={'Idempotency-Key': 'response-call'})
        assert response.output[0].type == 'function_call' and response.status == 'completed'
        result = await client.responses.create(model='auto', previous_response_id=response.id,
            input=[{'type': 'function_call_output', 'call_id': response.output[0].call_id, 'output': 'sunny'}],
            extra_headers={'Idempotency-Key': 'response-result'})
        assert result.output_text == 'sunny'
    assert captured[-1].messages[-1]['tool_call_id'] == 'call_weather'


@pytest.mark.asyncio
async def test_invalid_schema_result_replays_failure_and_keeps_measured_usage(openai_env, monkeypatch):
    from sqlalchemy import select

    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.shared.dtos.gateway import OutputEvidence
    from src.shared.models.runtime import RuntimeOperationRecord
    calls = []
    async def generate(self, prompt, *, model_id, **kwargs):
        calls.append(model_id)
        return ProviderResponse(text='{"wrong":true}', provider='offline', model_id=model_id,
            input_tokens=46, output_tokens=4, usage_received=True, output_evidence=OutputEvidence(finish_reason='stop'))
    monkeypatch.setattr(OfflineProvider, 'generate', generate)
    body = {'model': 'auto', 'messages': [{'role': 'user', 'content': 'json'}], 'response_format': {
        'type': 'json_schema', 'json_schema': {'name': 'Answer', 'schema': {'type': 'object',
            'properties': {'answer': {'type': 'string'}}, 'required': ['answer'], 'additionalProperties': False}}}}
    headers = {'Authorization': 'Bearer ' + openai_env['key'], 'Idempotency-Key': 'invalid-schema-replay'}
    first = await openai_env['client'].post('/v1/chat/completions', json=body, headers=headers)
    replay = await openai_env['client'].post('/v1/chat/completions', json=body, headers=headers)
    assert first.status_code == replay.status_code == 502
    assert first.json() == replay.json() and replay.headers['idempotency-replayed'] == 'true'
    assert len(calls) == 1
    async with openai_env['factory']() as session:
        operation = await session.scalar(select(RuntimeOperationRecord))
        assert operation.status == 'failed'
        assert operation.public_result_json['usage']['final_output']['total_tokens'] == 50


@pytest.mark.asyncio
async def test_unknown_function_output_is_terminal_failure(openai_env, monkeypatch):
    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.shared.dtos.gateway import OutputEvidence
    async def generate(self, prompt, *, model_id, **kwargs):
        return ProviderResponse(text='', provider='offline', model_id=model_id, usage_received=True,
            input_tokens=3, output_tokens=2, output_evidence=OutputEvidence(finish_reason='tool_calls'),
            metadata={'openai_choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-1', 'type': 'function',
                    'function': {'name': 'not_supplied', 'arguments': '{}'}}]}}]})
    monkeypatch.setattr(OfflineProvider, 'generate', generate)
    response = await openai_env['client'].post('/v1/chat/completions', headers={
        'Authorization': 'Bearer ' + openai_env['key'], 'Idempotency-Key': 'unknown-function'},
        json={'model': 'auto', 'messages': [{'role': 'user', 'content': 'lookup'}],
            'tools': [{'type': 'function', 'function': {'name': 'lookup'}}]})
    assert response.status_code == 502
    assert response.json()['error']['code'] == 'invalid_provider_response'


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', [None, {'index': 0, 'finish_reason': 'stop', 'message': None}])
async def test_malformed_choice_is_terminal_and_keeps_measured_usage(openai_env, monkeypatch, choice):
    from sqlalchemy import select

    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.shared.dtos.gateway import OutputEvidence
    from src.shared.models.runtime import RuntimeOperationRecord
    calls = []
    async def generate(self, prompt, *, model_id, **kwargs):
        calls.append(model_id)
        return ProviderResponse(text='known', provider='offline', model_id=model_id, input_tokens=46,
            output_tokens=4, usage_received=True, output_evidence=OutputEvidence(finish_reason='stop'),
            metadata={'openai_choices': [choice]})
    monkeypatch.setattr(OfflineProvider, 'generate', generate)
    request_headers = {'Authorization': 'Bearer ' + openai_env['key'], 'Idempotency-Key': 'malformed-choice'}
    body = {'model': 'auto', 'temperature': 0, 'messages': [{'role': 'user', 'content': 'hello'}]}
    first = await openai_env['client'].post('/v1/chat/completions', headers=request_headers, json=body)
    assert first.status_code == 502, first.text
    replay = await openai_env['client'].post('/v1/chat/completions', headers=request_headers, json=body)
    assert first.json() == replay.json() and len(calls) == 1
    async with openai_env['factory']() as session:
        operation = await session.scalar(select(RuntimeOperationRecord))
        assert operation.status == 'failed'
        assert operation.public_result_json['usage']['final_output']['total_tokens'] == 50
