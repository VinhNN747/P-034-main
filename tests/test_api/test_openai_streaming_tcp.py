import asyncio
import socket
import time

import pytest
import pytest_asyncio
import uvicorn
from openai import AsyncOpenAI
from sqlalchemy import func, select

from src.backend_ai.gateway.providers.base import ProviderResponse, ProviderStreamEvent
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.shared.dtos.gateway import OutputEvidence
from src.shared.models.billing import BillingTransactionRecord
from tests.service_app import app
from tests.test_api.test_openai_compat import openai_env as openai_env


@pytest_asyncio.fixture
async def streaming_gateway(openai_env, monkeypatch):
    done = asyncio.Event()
    calls = []
    openai_env["settings"].gateway_openai_real_streaming_enabled = True

    def result(model_id, n=1):
        return ProviderResponse(text=" hello world ", provider="offline", model_id=model_id,
            input_tokens=3*n, output_tokens=2*n, usage_received=True,
            output_evidence=OutputEvidence(finish_reason="stop"),
            metadata={'openai_choices':[{'index':index, 'finish_reason':'stop',
                'message':{'role':'assistant','content':' hello world '}} for index in range(n)]} if n > 1 else {})

    async def generate(self, prompt, *, model_id, **kwargs):
        calls.append("generate")
        await asyncio.sleep(1)
        done.set()
        return result(model_id)

    async def stream(self, prompt, *, model_id, **kwargs):
        calls.append("stream")
        n = kwargs['options'].n if kwargs.get('options') else 1
        await asyncio.sleep(0.1)
        for index in range(n):
            yield ProviderStreamEvent(type="text_delta", index=index, text=" hello")
        await asyncio.sleep(0.9)
        for index in range(n):
            yield ProviderStreamEvent(type="text_delta", index=index, text=" world ")
        done.set()
        yield ProviderStreamEvent(type="final", final=result(model_id, n))

    monkeypatch.setattr(OfflineProvider, "generate", generate)
    monkeypatch.setattr(OfflineProvider, "stream", stream)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(0.01)
    try:
        yield {**openai_env, "url": f"http://127.0.0.1:{port}/v1", "done": done, "calls": calls,
               "server": server, "server_task": task}
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


@pytest.mark.asyncio
async def test_sdk_tcp_first_delta_precedes_completion_and_replay_does_not_execute(streaming_gateway):
    env = streaming_gateway
    async with AsyncOpenAI(base_url=env["url"], api_key=env["key"], max_retries=0) as client:
        args = {"model": "auto:fast", "messages": [{"role": "user", "content": "Say hello"}],
            "stream": True, "stream_options": {"include_usage": True},
            "extra_headers": {"Idempotency-Key": "timed-stream"}}
        started = time.monotonic()
        chunks = []
        async for chunk in await client.chat.completions.create(**args):
            if chunk.choices and chunk.choices[0].delta.content and not chunks:
                assert time.monotonic() - started < 0.5
                assert not env["done"].is_set()
            chunks.append(chunk)
        assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == " hello world "
        assert chunks[-2].choices[0].finish_reason == "stop"
        assert chunks[-1].usage.total_tokens == 5
        replay = [chunk async for chunk in await client.chat.completions.create(**args)]
        assert replay[0].id == chunks[0].id
        assert env["calls"] == ["stream"]
    async with env["factory"]() as session:
        # One logical runtime debit owner; zero-price fixture can suppress a debit row.
        assert await session.scalar(select(func.count()).select_from(BillingTransactionRecord)) <= 1


@pytest.mark.asyncio
async def test_sdk_stream_disconnect_makes_receipt_indeterminate_and_never_reexecutes(streaming_gateway):
    env = streaming_gateway
    from openai import ConflictError

    from src.shared.models.runtime import RuntimeOperationRecord
    async with AsyncOpenAI(base_url=env["url"], api_key=env["key"], max_retries=0) as client:
        args = {"model": "auto:fast", "messages": [{"role": "user", "content": "Say hello"}],
            "stream": True, "extra_headers": {"Idempotency-Key": "cancel-stream"}}
        stream = await client.chat.completions.create(**args)
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                break
        await stream.close()
        for _ in range(100):
            async with env["factory"]() as session:
                operation = await session.scalar(select(RuntimeOperationRecord))
                if operation.status == "indeterminate":
                    break
            await asyncio.sleep(0.01)
        assert operation.status == "indeterminate"
        with pytest.raises(ConflictError):
            await client.chat.completions.create(**args)
        assert env["calls"] == ["stream"]


@pytest.mark.asyncio
async def test_responses_sdk_tcp_delta_precedes_completion_and_stored_replay(streaming_gateway):
    env = streaming_gateway
    async with AsyncOpenAI(base_url=env['url'], api_key=env['key'], max_retries=0,
                           _strict_response_validation=True) as client:
        args = {'model': 'auto:fast', 'input': 'Say hello', 'stream': True,
                'extra_headers': {'Idempotency-Key': 'responses-timed'}}
        started = time.monotonic()
        events = []
        async for event in await client.responses.create(**args):
            if event.type == 'response.output_text.delta' and not any(
                previous.type == 'response.output_text.delta' for previous in events):
                assert time.monotonic() - started < 0.5
                assert not env['done'].is_set()
            events.append(event)
        assert ''.join(e.delta for e in events if e.type == 'response.output_text.delta') == ' hello world '
        assert events[-1].type == 'response.completed'
        assert [e.sequence_number for e in events] == list(range(len(events)))
        stored = await client.responses.retrieve(events[-1].response.id)
        assert stored.output_text == ' hello world '
        replay = [event async for event in await client.responses.create(**args)]
        assert replay[-1].response.id == stored.id
        assert env['calls'] == ['stream']


@pytest.mark.asyncio
async def test_disconnect_preserves_received_usage_and_one_debit(streaming_gateway, monkeypatch):
    from sqlalchemy import update

    from src.shared.models.gateway import Model
    from src.shared.models.runtime import RuntimeOperationRecord
    env = streaming_gateway
    async with env['factory']() as session:
        await session.execute(update(Model).values(input_price_per_million=1, output_price_per_million=1))
        await session.commit()
    async def measured_stream(self, prompt, *, model_id, **kwargs):
        env['calls'].append('stream')
        yield ProviderStreamEvent(type='text_delta', text=' hello')
        yield ProviderStreamEvent(type='usage', final=ProviderResponse(text=' hello', provider='offline',
            model_id=model_id, input_tokens=3, output_tokens=2, usage_received=True))
        await asyncio.Event().wait()
    monkeypatch.setattr(OfflineProvider, 'stream', measured_stream)
    async with AsyncOpenAI(base_url=env['url'], api_key=env['key'], max_retries=0) as client:
        stream = await client.chat.completions.create(model='auto:fast',
            messages=[{'role':'user','content':'Say hello'}], stream=True,
            extra_headers={'Idempotency-Key':'cancel-measured'})
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                break
        await stream.close()
        for _ in range(150):
            async with env['factory']() as session:
                operation = await session.scalar(select(RuntimeOperationRecord))
                if operation.status == 'indeterminate':
                    break
            await asyncio.sleep(0.01)
        assert operation.status == 'indeterminate'
        assert operation.public_result_json['usage']['execution']['total_tokens'] == 5
        assert operation.public_result_json['usage']['complete'] is False
        async with env['factory']() as session:
            assert await session.scalar(select(func.count()).select_from(BillingTransactionRecord)) == 1


@pytest.mark.asyncio
async def test_streamed_mixed_items_match_stored_response_order(streaming_gateway, monkeypatch):
    async def mixed(self, prompt, *, model_id, **kwargs):
        call = {'id':'call_order','type':'function','function':{'name':'lookup','arguments':'{}'}}
        yield ProviderStreamEvent(type='tool_delta', tool_delta={'index':0, **call})
        yield ProviderStreamEvent(type='text_delta', text=' hello')
        yield ProviderStreamEvent(type='final', final=ProviderResponse(text=' hello',provider='offline',model_id=model_id,
            input_tokens=3,output_tokens=2,usage_received=True,output_evidence=OutputEvidence(finish_reason='tool_calls'),
            metadata={'openai_choices':[{'index':0,'finish_reason':'tool_calls','message':{
                'role':'assistant','content':' hello','tool_calls':[call]}}]}))
    monkeypatch.setattr(OfflineProvider, 'stream', mixed)
    env = streaming_gateway
    async with AsyncOpenAI(base_url=env['url'], api_key=env['key'], max_retries=0,
                           _strict_response_validation=True) as client:
        events = [event async for event in await client.responses.create(model='auto',input='Look up',
            tools=[{'type':'function','name':'lookup'}],stream=True,
            extra_headers={'Idempotency-Key':'mixed-order'})]
        final = events[-1].response
        stored = await client.responses.retrieve(final.id)
        assert [item.type for item in final.output] == ['function_call','message']
        assert stored.model_dump() == final.model_dump()


@pytest.mark.asyncio
async def test_late_stream_transport_error_preserves_measured_usage_and_debit(streaming_gateway, monkeypatch):
    from openai import APIError, APIStatusError
    from sqlalchemy import update

    from src.backend_ai.gateway.providers.base import TransientProviderError
    from src.shared.models.gateway import Model
    from src.shared.models.runtime import RuntimeOperationRecord
    env = streaming_gateway
    async with env['factory']() as session:
        await session.execute(update(Model).values(input_price_per_million=1, output_price_per_million=1))
        await session.commit()
    async def measured(self, prompt, *, model_id, **kwargs):
        env['calls'].append('stream')
        yield ProviderStreamEvent(type='text_delta', text=' hello')
        yield ProviderStreamEvent(type='usage', final=ProviderResponse(text=' hello', provider='offline',
            model_id=model_id, input_tokens=3, output_tokens=2, usage_received=True))
        raise TransientProviderError('private-provider-address')
    monkeypatch.setattr(OfflineProvider, 'stream', measured)
    async with AsyncOpenAI(base_url=env['url'], api_key=env['key'], max_retries=0) as client:
        args = {'model':'auto:fast', 'messages':[{'role':'user','content':'Hello'}], 'stream':True,
                'extra_headers':{'Idempotency-Key':'late-measured-fault'}}
        with pytest.raises(APIError):
            async for _ in await client.chat.completions.create(**args):
                pass
        with pytest.raises(APIStatusError):
            await client.chat.completions.create(**args)
    async with env['factory']() as session:
        operation = await session.scalar(select(RuntimeOperationRecord))
        assert operation.status in {'failed', 'indeterminate'}
        assert operation.public_result_json['usage']['execution']['total_tokens'] == 5
        assert operation.public_result_json['usage']['complete'] is False
        assert await session.scalar(select(func.count()).select_from(BillingTransactionRecord)) == 1
    assert env['calls'] == ['stream']


@pytest.mark.asyncio
async def test_server_shutdown_cancels_active_stream_and_retains_receipt(streaming_gateway, monkeypatch):
    from sqlalchemy import update

    from src.shared.models.gateway import Model
    from src.shared.models.runtime import RuntimeOperationRecord
    env = streaming_gateway
    async with env['factory']() as session:
        await session.execute(update(Model).values(input_price_per_million=1, output_price_per_million=1))
        await session.commit()
    measured, cancelled = asyncio.Event(), asyncio.Event()
    async def held(self, prompt, *, model_id, **kwargs):
        env['calls'].append('stream')
        try:
            yield ProviderStreamEvent(type='text_delta', text=' hello')
            yield ProviderStreamEvent(type='usage', final=ProviderResponse(text=' hello', provider='offline',
                model_id=model_id, input_tokens=3, output_tokens=2, usage_received=True))
            measured.set()
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(OfflineProvider, 'stream', held)
    args = {'model':'auto:fast', 'messages':[{'role':'user','content':'Hello'}], 'stream':True}
    headers = {'Authorization':'Bearer '+env['key'], 'Idempotency-Key':'shutdown-held-stream'}
    async with AsyncOpenAI(base_url=env['url'], api_key=env['key'], max_retries=0) as client:
        stream = await client.chat.completions.create(**args,
            extra_headers={'Idempotency-Key':headers['Idempotency-Key']})
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                break
        await asyncio.wait_for(measured.wait(), 2)
        env['server'].config.timeout_graceful_shutdown = 0.2
        env['server'].should_exit = True
        await asyncio.wait_for(env['server_task'], 5)
        await asyncio.wait_for(cancelled.wait(), 2)
        await stream.close()
    for _ in range(150):
        async with env['factory']() as session:
            operation = await session.scalar(select(RuntimeOperationRecord))
            if operation.status == 'indeterminate':
                break
        await asyncio.sleep(0.01)
    async with env['factory']() as session:
        assert operation.status == 'indeterminate'
        assert operation.public_result_json['usage']['execution']['total_tokens'] == 5
        assert operation.public_result_json['usage']['complete'] is False
        assert await session.scalar(select(func.count()).select_from(BillingTransactionRecord)) == 1
    # A fresh request/session after the TCP server has stopped uses the durable
    # operation, rather than resuming the cancelled producer or debiting again.
    replay = await env['client'].post('/v1/chat/completions', json=args, headers=headers)
    assert replay.status_code == 409 and replay.json()['error']['code'] == 'request_outcome_unknown'
    assert replay.headers['x-request-id'] == operation.request_id
    assert env['calls'] == ['stream']
