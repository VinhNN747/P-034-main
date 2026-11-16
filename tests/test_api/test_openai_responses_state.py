import pytest
from sqlalchemy import func, select, update

from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.shared.models import GatewayRequestRecord
from tests.test_api.test_openai_compat import openai_env as openai_env


def headers(key, operation=None):
    value = {'Authorization': 'Bearer ' + key}
    if operation:
        value['Idempotency-Key'] = operation
    return value


@pytest.mark.asyncio
async def test_responses_default_store_history_isolation_delete_and_replay(openai_env, monkeypatch):
    captured = []
    original = OfflineProvider.generate

    async def generate(self, prompt, **kwargs):
        captured.append(kwargs.get('options'))
        return await original(self, prompt, **kwargs)

    monkeypatch.setattr(OfflineProvider, 'generate', generate)
    client, key = openai_env['client'], openai_env['key']
    body = {'model': 'auto', 'input': 'First turn', 'metadata': {'case': 'state'}}
    first = await client.post('/v1/responses', json=body, headers=headers(key, 'state-first'))
    assert first.status_code == 200, first.text
    response_id = first.json()['id']
    retrieved = await client.get('/v1/responses/' + response_id, headers=headers(key))
    assert retrieved.status_code == 200 and retrieved.json() == first.json()
    foreign = await client.get('/v1/responses/' + response_id, headers=headers(openai_env['other_key']))
    assert foreign.status_code == 404
    second = await client.post('/v1/responses', json={'model': 'auto', 'input': 'Next turn',
        'previous_response_id': response_id, 'instructions': 'Only this turn'}, headers=headers(key, 'state-next'))
    assert second.status_code == 200, second.text
    messages = captured[-1].messages
    assert [m['role'] for m in messages] == ['developer', 'user', 'assistant', 'user']
    assert messages[1]['content'] == 'First turn' and messages[-1]['content'] == 'Next turn'
    assert first.json()['output'][0]['content'][0]['text'] == messages[2]['content']
    async with openai_env['factory']() as session:
        before = await session.scalar(select(func.count()).select_from(GatewayRequestRecord))
    deleted = await client.delete('/v1/responses/' + response_id, headers=headers(key))
    assert deleted.status_code == 200 and deleted.json()['deleted'] is True
    assert (await client.get('/v1/responses/' + response_id, headers=headers(key))).status_code == 404
    replay = await client.post('/v1/responses', json=body, headers=headers(key, 'state-first'))
    assert replay.status_code == 200 and replay.json() == first.json()
    assert replay.headers['idempotency-replayed'] == 'true'
    assert (await client.get('/v1/responses/' + response_id, headers=headers(key))).status_code == 404
    async with openai_env['factory']() as session:
        assert await session.scalar(select(func.count()).select_from(GatewayRequestRecord)) == before


@pytest.mark.asyncio
async def test_responses_store_false_and_expiry_are_not_public_retrievable(openai_env):
    from datetime import timedelta

    from src.shared.models.core import utc_now

    client, key = openai_env['client'], openai_env['key']
    stateless = await client.post('/v1/responses', json={'model': 'auto', 'input': 'Private turn', 'store': False},
        headers=headers(key, 'state-false'))
    assert stateless.status_code == 200
    assert (await client.get('/v1/responses/' + stateless.json()['id'], headers=headers(key))).status_code == 404
    stored = await client.post('/v1/responses', json={'model': 'auto', 'input': 'Expire me'},
        headers=headers(key, 'state-expired'))
    assert stored.status_code == 200
    from src.shared.models.openai_responses import StoredOpenAIResponse
    async with openai_env['factory']() as session:
        await session.execute(update(StoredOpenAIResponse).where(StoredOpenAIResponse.id == stored.json()['id'])
            .values(expires_at=utc_now() - timedelta(seconds=1)))
        await session.commit()
    assert (await client.get('/v1/responses/' + stored.json()['id'], headers=headers(key))).status_code == 404
    rejected = await client.post('/v1/responses', json={'model': 'auto', 'input': 'Continue',
        'previous_response_id': stored.json()['id']}, headers=headers(key, 'state-expired-next'))
    assert rejected.status_code == 404 and rejected.json()['error']['param'] == 'previous_response_id'


@pytest.mark.asyncio
async def test_completed_child_replays_after_previous_response_deleted(openai_env):
    client, key = openai_env['client'], openai_env['key']
    first = await client.post('/v1/responses', json={'model': 'auto', 'input': 'First'}, headers=headers(key, 'parent'))
    body = {'model': 'auto', 'input': 'Next', 'previous_response_id': first.json()['id']}
    child = await client.post('/v1/responses', json=body, headers=headers(key, 'child'))
    assert child.status_code == 200
    await client.delete('/v1/responses/' + first.json()['id'], headers=headers(key))
    replay = await client.post('/v1/responses', json=body, headers=headers(key, 'child'))
    assert replay.status_code == 200 and replay.json() == child.json()
    assert replay.headers['idempotency-replayed'] == 'true'


@pytest.mark.asyncio
async def test_store_failure_replay_recovers_history_after_parent_deleted(openai_env, monkeypatch):
    from src.backend_ai.routers.openai_errors import OpenAIAPIError
    from src.backend_ai.services.openai_response_store import OpenAIResponseStore
    from src.shared.models.openai_responses import StoredOpenAIResponse
    client, key = openai_env['client'], openai_env['key']
    parent = await client.post('/v1/responses', json={'model': 'auto', 'input': 'Original turn'},
        headers=headers(key, 'recovery-parent'))
    original = OpenAIResponseStore.save
    async def fail(self, *args):
        raise OpenAIAPIError(503, 'Injected store outage.', code='response_storage_unavailable')
    monkeypatch.setattr(OpenAIResponseStore, 'save', fail)
    body = {'model': 'auto', 'input': 'Next turn', 'previous_response_id': parent.json()['id']}
    failed = await client.post('/v1/responses', json=body, headers=headers(key, 'recovery-child'))
    assert failed.status_code == 503
    await client.delete('/v1/responses/' + parent.json()['id'], headers=headers(key))
    monkeypatch.setattr(OpenAIResponseStore, 'save', original)
    replay = await client.post('/v1/responses', json=body, headers=headers(key, 'recovery-child'))
    assert replay.status_code == 200 and replay.headers['idempotency-replayed'] == 'true'
    async with openai_env['factory']() as session:
        stored = await session.get(StoredOpenAIResponse, replay.json()['id'])
        assert [item['content'] for item in stored.history_json if item['role'] == 'user'] == ['Original turn', 'Next turn']


@pytest.mark.asyncio
async def test_mixed_response_history_keeps_one_assistant_turn(openai_env, monkeypatch):
    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.shared.dtos.gateway import OutputEvidence

    captured = []
    async def generate(self, prompt, *, model_id, options, **kwargs):
        captured.append(options.messages)
        message = {'role': 'assistant', 'content': 'I will look it up.', 'tool_calls': [
            {'id': 'call_mixed', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]}
        if options.messages[-1]['role'] == 'tool':
            message = {'role': 'assistant', 'content': 'Found it.'}
        reason = 'tool_calls' if message.get('tool_calls') else 'stop'
        return ProviderResponse(text=message['content'], provider='offline', model_id=model_id,
            input_tokens=3, output_tokens=2, usage_received=True, output_evidence=OutputEvidence(finish_reason=reason),
            metadata={'openai_choices': [{'index': 0, 'finish_reason': reason, 'message': message}]})

    monkeypatch.setattr(OfflineProvider, 'generate', generate)
    client, key = openai_env['client'], openai_env['key']
    first = await client.post('/v1/responses', headers=headers(key, 'mixed-history'), json={
        'model': 'auto', 'input': 'Look up', 'tools': [{'type': 'function', 'name': 'lookup'}]})
    assert first.status_code == 200, first.text
    next_turn = await client.post('/v1/responses', headers=headers(key, 'mixed-history-next'), json={
        'model': 'auto', 'previous_response_id': first.json()['id'],
        'input': [{'type': 'function_call_output', 'call_id': 'call_mixed', 'output': 'Found'}]})
    assert next_turn.status_code == 200, next_turn.text
    assistant = [item for item in captured[-1] if item['role'] == 'assistant']
    assert len(assistant) == 1
    assert assistant[0]['content'] == 'I will look it up.'
    assert assistant[0]['tool_calls'][0]['id'] == 'call_mixed'


@pytest.mark.asyncio
async def test_stored_refusal_history_can_continue_without_losing_refusal(openai_env, monkeypatch):
    from src.backend_ai.gateway.providers.base import ProviderResponse
    from src.shared.dtos.gateway import OutputEvidence
    captured = []
    async def generate(self, prompt, *, model_id, options, **kwargs):
        captured.append(options.messages)
        message = ({'role': 'assistant', 'content': None, 'refusal': 'I cannot provide that.'}
                   if len(options.messages) == 1 else {'role': 'assistant', 'content': 'A safe alternative.'})
        return ProviderResponse(text=message['content'] or '', provider='offline', model_id=model_id,
            input_tokens=3, output_tokens=2, usage_received=True, output_evidence=OutputEvidence(finish_reason='stop'),
            metadata={'openai_choices': [{'index':0, 'finish_reason':'stop', 'message':message}]})
    monkeypatch.setattr(OfflineProvider, 'generate', generate)
    client, key = openai_env['client'], openai_env['key']
    first = await client.post('/v1/responses', headers=headers(key, 'refusal-history'), json={
        'model':'auto', 'input':'Initial turn', 'temperature':0})
    assert first.status_code == 200 and first.json()['output'][0]['content'][0]['type'] == 'refusal'
    followup = await client.post('/v1/responses', headers=headers(key, 'refusal-followup'), json={
        'model':'auto', 'input':'A safe alternative, please.', 'previous_response_id':first.json()['id']})
    assert followup.status_code == 200, followup.text
    assert captured[-1][1]['refusal'] == 'I cannot provide that.'
    assert captured[-1][1]['content'] is None
