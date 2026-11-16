import sys
from types import ModuleType

from src.backend_ai.gateway.providers.openai import REASONING_EFFORT, OpenAIProvider, split_effort
from src.backend_ai.gateway.providers.openrouter import OpenRouterProvider


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = ModuleType("langchain_openai")
    module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", module)
    return captured


def test_split_effort_only_takes_known_efforts():
    assert split_effort("gpt-6.1-sol@high") == ("gpt-6.1-sol", "high")
    assert split_effort("gpt-6.1-sol") == ("gpt-6.1-sol", None)
    assert split_effort("gpt-6.1-sol@2026-10") == ("gpt-6.1-sol@2026-10", None)
    assert split_effort("@high") == ("@high", None)


def test_effort_alias_calls_the_base_model_at_that_effort(monkeypatch):
    captured = _capture(monkeypatch)
    OpenAIProvider(api_key="k", enabled=True)._client_for("gpt-6.1-sol@high", 4, None)
    assert captured["model"] == "gpt-6.1-sol"
    assert captured["reasoning_effort"] == "high"


def test_plain_name_keeps_the_default_effort(monkeypatch):
    captured = _capture(monkeypatch)
    OpenAIProvider(api_key="k", enabled=True)._client_for("gpt-6.1-sol", 4, None)
    assert captured["model"] == "gpt-6.1-sol"
    assert captured["reasoning_effort"] == REASONING_EFFORT


def test_openrouter_strips_the_alias_and_still_sends_no_effort(monkeypatch):
    captured = _capture(monkeypatch)
    OpenRouterProvider(api_key="k")._client_for("openrouter/deepseek/deepseek-v4-pro@high", 4, None)
    assert captured["model"] == "deepseek/deepseek-v4-pro"
    assert "reasoning_effort" not in captured
