import sys
from types import ModuleType, SimpleNamespace

import pytest

from src.backend_ai.agents.routing.router import _build_model_choices
from src.backend_ai.gateway.llm.client import LangChainOpenAIClient
from src.backend_ai.gateway.providers.openai import OpenAIProvider, _output_evidence


def test_upstream_retry_count_is_owned_by_runtime(monkeypatch):
    captured = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    module = ModuleType("langchain_openai")
    module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", module)

    OpenAIProvider(enabled=True, api_key="local-test-key")._client_for("m", 3)
    LangChainOpenAIClient("local-test-key")._make_llm("m", 3)

    _build_model_choices(
        3,
        settings=SimpleNamespace(
            gateway_model_tier_small_fast="small",
            gateway_model_tier_mid="mid",
            gateway_model_tier_large_reasoning="large",
        ),
        api_key="local-test-key",
    )

    assert len(captured) == 5
    assert all(client_kwargs["max_retries"] == 0 for client_kwargs in captured)


def test_openai_finish_and_usage_details_keep_only_measured_values():
    response = SimpleNamespace(
        response_metadata={
            "finish_reason": "length",
            "token_usage": {
                "prompt_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 2},
                "completion_tokens_details": {"reasoning_tokens": 1},
            },
        },
        usage_metadata={},
    )
    evidence = _output_evidence(response)
    assert evidence.finish_reason == "length"
    assert evidence.cached_tokens == 4
    assert evidence.cache_write_tokens == 2
    assert evidence.reasoning_tokens == 1


def test_unknown_finish_reason_and_missing_details_stay_unknown():
    response = SimpleNamespace(response_metadata={}, usage_metadata={})
    evidence = _output_evidence(response)
    assert evidence.finish_reason == "unknown"
    assert evidence.cached_tokens is None
    assert evidence.cache_write_tokens is None
    assert evidence.reasoning_tokens is None


@pytest.mark.parametrize('usage', [{'input_tokens':3}, {'output_tokens':2},
    {'input_tokens':True,'output_tokens':2}, {'input_tokens':3,'output_tokens':-1}])
def test_partial_or_invalid_core_usage_is_not_marked_measured(usage):
    from src.backend_ai.gateway.providers.openai import _has_usage
    assert not _has_usage(SimpleNamespace(usage_metadata=usage, response_metadata={}))


def test_reasoning_model_preserves_public_hard_completion_cap(monkeypatch):
    captured = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    module = ModuleType('langchain_openai')
    module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, 'langchain_openai', module)
    provider = OpenAIProvider(enabled=True, api_key='local-test-key')
    provider._client_for('gpt-5.4-nano', 3, 48, hard_completion_token_limit=48)
    assert captured[0]['max_tokens'] == 48


def test_hard_completion_cap_takes_lower_application_limit(monkeypatch):
    captured = []
    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)
    module = ModuleType('langchain_openai')
    module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, 'langchain_openai', module)
    OpenAIProvider(enabled=True, api_key='local-test-key')._client_for(
        'gpt-5.4-nano', 3, 24, hard_completion_token_limit=48)
    assert captured[0]['max_tokens'] == 24


@pytest.mark.asyncio
async def test_internal_task_does_not_spend_shared_public_completion_budget(monkeypatch):
    from src.backend_ai.agents.routing.executor import SubtaskExecutor
    from src.backend_ai.agents.routing.router import ExecutionConfig, ExecutionResult
    from src.shared.dtos.gateway import SubTask

    executor = SubtaskExecutor(SimpleNamespace(), execution_config=ExecutionConfig(
        max_output_tokens=8, hard_completion_token_limit=8, shared_completion_budget=True,
        stream_task_ids=('final',)))
    caps = []
    async def execute(task, *args, **kwargs):
        caps.append(executor._execution_config.hard_completion_token_limit)
        return ExecutionResult(task_id=task.task_id, status='success', result='ok',
            selected_model='fixture', executed_model='fixture', fallback=False,
            usage_complete=True, output_tokens=8 if task.task_id == 'internal' else 2)
    monkeypatch.setattr(executor, 'aexecute_subtask', execute)
    await executor.aexecute_subtasks([
        SubTask(task_id='internal', description='prepare', expected_output='context', task_type='general', difficulty='easy'),
        SubTask(task_id='final', description='answer', expected_output='text', task_type='general', difficulty='easy', dependencies=['internal'])])
    assert caps == [8, 8]


@pytest.mark.asyncio
async def test_wasted_retry_tokens_do_not_spend_final_concatenation_budget(monkeypatch):
    from src.backend_ai.agents.routing.executor import SubtaskExecutor
    from src.backend_ai.agents.routing.router import ExecutionConfig, ExecutionResult
    from src.shared.dtos.gateway import SubTask
    executor = SubtaskExecutor(SimpleNamespace(), execution_config=ExecutionConfig(
        max_output_tokens=8, hard_completion_token_limit=8, shared_completion_budget=True,
        stream_task_ids=('first', 'second')))
    caps = []
    async def execute(task, *args, **kwargs):
        caps.append(executor._execution_config.hard_completion_token_limit)
        return ExecutionResult(task_id=task.task_id, status='success', result='ok',
            selected_model='fixture', executed_model='fixture', fallback=False,
            usage_complete=True, final_usage_complete=True, output_tokens=7, final_output_tokens=2)
    monkeypatch.setattr(executor, 'aexecute_subtask', execute)
    await executor.aexecute_subtasks([SubTask(task_id=task, description='answer', expected_output='text',
        task_type='general', difficulty='easy') for task in ('first', 'second')])
    assert caps == [8, 6]
