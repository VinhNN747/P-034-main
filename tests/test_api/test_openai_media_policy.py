import pytest

from tests.test_api.test_openai_compat import PNG
from tests.test_api.test_openai_compat import openai_env as openai_env


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True])
async def test_media_is_not_silently_dropped_before_generation(openai_env, remote):
    import base64

    url = "https://example.test/image.png" if remote else "data:image/png;base64," + base64.b64encode(PNG).decode()
    response = await openai_env["client"].post("/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe this picture"},
            {"type": "image_url", "image_url": {"url": url}}]}]},
        headers={"Authorization": "Bearer " + openai_env["key"], "Idempotency-Key": "media-policy"})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] in {"unsupported_media", "attachment_analysis_failed"}
    assert response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_explicit_partial_media_policy_returns_public_warning(openai_env):
    response = await openai_env["client"].post("/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Say hello"},
            {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}}]}],
        "gateway": {"allow_partial_response": True}},
        headers={"Authorization": "Bearer " + openai_env["key"], "Idempotency-Key": "partial-media"})
    assert response.status_code == 200, response.text
    assert "ATTACHMENT_OMITTED" in response.json()["gateway_warnings"]


def test_explicit_partial_does_not_bypass_analysis_size_limit():
    import base64
    from types import SimpleNamespace

    from src.backend_ai.services.openai_media_policy import preflight_media
    from src.shared.dtos.gateway import GatewayRequest, RequestConstraints
    from src.shared.dtos.runtime_api import RuntimeAPIError
    request = GatewayRequest(request_id='media-size', application_id='app',
        messages=[{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(PNG).decode()}}]}],
        constraints=RequestConstraints(allow_partial_response=True))
    with pytest.raises(RuntimeAPIError) as error:
        preflight_media(request, SimpleNamespace(limits=SimpleNamespace(max_bytes=2)))
    assert error.value.status_code == 413
