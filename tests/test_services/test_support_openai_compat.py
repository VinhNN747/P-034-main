"""Provider compatibility must not silently disable document understanding."""

import json

import httpx
import pytest

from src.backend_ai.services.support_openai_client import SupportOpenAIClient, SupportOpenAIError
from src.shared.dtos.ai_internal import SupportGroundingVerdict


@pytest.mark.asyncio
async def test_temperature_rejection_retries_without_it_and_caches_for_next_client():
    requests = []

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "temperature" in payload:
            return httpx.Response(400, json={"error": {
                "code": "unsupported_value", "param": "temperature", "message": "private-provider-details",
            }})
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": '{"supported": true}'},
        }]})

    for _ in range(2):
        client = SupportOpenAIClient(
            api_key="test", model="temperature-default-only", timeout=1,
            base_url="https://compat-test.invalid/v1", transport=httpx.MockTransport(provider),
        )
        result = await client.structured(
            system="Verify evidence", data="Public FAQ", schema=SupportGroundingVerdict, temperature=0.45,
        )
        assert result.supported
    assert len(requests) == 3
    assert requests[0]["temperature"] == 0.45
    assert all("temperature" not in payload for payload in requests[1:])
    assert all(payload["response_format"]["json_schema"]["strict"] for payload in requests)


@pytest.mark.asyncio
async def test_model_or_credential_errors_are_not_retried_and_do_not_echo_provider_message():
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(401, json={"error": {
            "code": "invalid_api_key", "message": "sk-proj-private-key user-data",
        }})

    client = SupportOpenAIClient(api_key="test", model="invalid-auth", timeout=1, transport=httpx.MockTransport(provider))
    with pytest.raises(SupportOpenAIError) as caught:
        await client.structured(system="Verify evidence", data="Public FAQ", schema=SupportGroundingVerdict)
    assert "invalid_api_key" in str(caught.value)
    assert "sk-proj" not in str(caught.value)
    assert "user-data" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported", [False, True])
async def test_reasoning_keeps_model_and_schema_with_optional_parameter_compatibility(unsupported):
    requests = []

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if unsupported and "reasoning_effort" in payload:
            return httpx.Response(400, json={"error": {
                "code": "unsupported_parameter", "param": "reasoning_effort",
            }})
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": '{"supported": true}'},
        }]})

    for _ in range(2):
        client = SupportOpenAIClient(
            api_key="test", model=f"reasoning-compat-{unsupported}", timeout=1,
            base_url="https://reasoning-test.invalid/v1", transport=httpx.MockTransport(provider),
        )
        result = await client.structured(
            system="Verify evidence", data="Public docs", schema=SupportGroundingVerdict,
            reasoning_effort="medium", max_tokens=6000,
        )
        assert result.supported
    assert len(requests) == (3 if unsupported else 2)
    assert requests[0]["reasoning_effort"] == "medium"
    assert "temperature" not in requests[0]
    assert all(payload["model"] == f"reasoning-compat-{unsupported}" for payload in requests)
    assert all(payload["max_completion_tokens"] == 6000 for payload in requests)
    assert all(payload["response_format"]["json_schema"]["strict"] for payload in requests)
    if unsupported:
        assert all("reasoning_effort" not in payload for payload in requests[1:])
    else:
        assert all(payload["reasoning_effort"] == "medium" for payload in requests)
