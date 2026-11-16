import sys
from types import ModuleType

import pytest

from src.backend_ai.gateway.factory import build_providers
from src.backend_ai.gateway.providers.base import REASONING_TOKEN_HEADROOM, ProviderConfigurationError
from src.backend_ai.gateway.providers.openrouter import OPENROUTER_BASE_URL, OpenRouterProvider
from src.shared.config import Settings


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = ModuleType("langchain_openai")
    module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", module)
    return captured


def test_catalog_prefix_is_stripped_and_openai_only_params_are_not_sent(monkeypatch):
    captured = _capture(monkeypatch)
    OpenRouterProvider(api_key="k")._client_for("openrouter/deepseek/deepseek-v4-pro", 4, 100)
    assert captured["model"] == "deepseek/deepseek-v4-pro"
    assert captured["base_url"] == OPENROUTER_BASE_URL
    assert "reasoning_effort" not in captured
    assert captured["max_tokens"] == 100 + REASONING_TOKEN_HEADROOM


def test_a_name_without_the_prefix_is_passed_through(monkeypatch):
    captured = _capture(monkeypatch)
    OpenRouterProvider(api_key="k")._client_for("openai/gpt-5.6-sol", 4, None)
    assert captured["model"] == "openai/gpt-5.6-sol"
    assert "reasoning_effort" not in captured


def test_missing_key_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ProviderConfigurationError, match="OpenRouter"):
        OpenRouterProvider(api_key="")


def test_factory_never_builds_live_provider_clients_without_request_bindings():
    kinds = {"model-1": "openrouter", "openrouter/deepseek/deepseek-v4-flash": "openrouter"}
    live = build_providers(Settings(gateway_execution_mode="live"), model_provider_kinds=kinds)
    assert live == {}

    offline = build_providers(Settings(gateway_execution_mode="offline"), model_provider_kinds=kinds)
    assert set(offline) == {"offline"}


def test_openrouter_sdk_cap_does_not_add_native_reasoning_headroom(monkeypatch):
    captured = _capture(monkeypatch)
    OpenRouterProvider(api_key="fixture")._client_for(
        "openrouter/deepseek/deepseek-v4-pro", 4, 48, hard_completion_token_limit=48)
    assert captured["model"] == "deepseek/deepseek-v4-pro"
    assert captured["max_tokens"] == 48
    assert "reasoning_effort" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_openrouter_message_protocol_strips_prefix_and_keeps_receipt_identity(monkeypatch, streaming):
    import json

    import httpx
    import openai

    from src.backend_ai.gateway.providers.base import GenerationOptions
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        usage = {'prompt_tokens':3, 'completion_tokens':2, 'total_tokens':5}
        if body.get('stream'):
            chunks = [
                {'id':'fixture', 'object':'chat.completion.chunk','created':0,'model':body['model'],
                 'choices':[{'index':0,'delta':{'role':'assistant','content':'hello'},'finish_reason':None}]},
                {'id':'fixture','object':'chat.completion.chunk','created':0,'model':body['model'],
                 'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]},
                {'id':'fixture','object':'chat.completion.chunk','created':0,'model':body['model'],
                 'choices':[],'usage':usage},
            ]
            wire=''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, content=wire)
        return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
            'choices':[{'index':0,'message':{'role':'assistant','content':'hello'},'finish_reason':'stop'}],
            'usage':usage})
    sdk = openai.AsyncOpenAI
    monkeypatch.setattr(openai,'AsyncOpenAI',lambda **kwargs: sdk(**kwargs,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))))
    provider=OpenRouterProvider(api_key='fixture')
    model='openrouter/deepseek/deepseek-v4-pro'
    options=GenerationOptions(messages=({'role':'user','content':'Hello'},),
                              max_output_tokens=48,hard_completion_token_limit=48,temperature=0)
    kwargs=dict(task_id='task',model_id=model,timeout_seconds=5,options=options)
    if streaming:
        events=[event async for event in provider.stream('Hello',**kwargs)]
        response=events[-1].final
        assert ''.join(event.text for event in events if event.type=='text_delta')=='hello'
        assert all(event.final.provider=='openrouter' for event in events if event.final)
    else:
        response=await provider.generate('Hello',**kwargs)
    assert requests[0]['model']=='deepseek/deepseek-v4-pro'
    assert requests[0]['max_completion_tokens']==48 and requests[0]['temperature']==0
    assert 'reasoning_effort' not in requests[0]
    assert response.model_id==model and response.provider=='openrouter'
    assert response.input_tokens==3 and response.output_tokens==2 and response.usage_received
