from __future__ import annotations

import base64
import json
import struct
import time
import zlib

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from src.shared.models.latency import LatencyObservationRecord
from tests.latency_support import count_latency_observations, wait_for_campaign
from tests.service_app import app
from tests.test_gateway.test_multimodal import _pdf

pytest_plugins = ("tests.latency_support",)


def _small_png() -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _sdk(env, *, strict_response_validation=True):
    from openai import AsyncOpenAI

    transport = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return AsyncOpenAI(
        base_url="http://test/v1",
        api_key=env["key"],
        http_client=transport,
        max_retries=0,
        _strict_response_validation=strict_response_validation,
    )


async def _observations(factory):
    async with factory() as session:
        return list((await session.scalars(
            select(LatencyObservationRecord).order_by(LatencyObservationRecord.dispatched_at)
        )).all())


def test_sdk_parameter_inventories_have_a_case_for_every_declared_parameter():
    from src.backend_ai.services.openai_compatibility_profile import STRICT_CHAT_PROFILE
    from src.backend_ai.services.openai_responses_validation import RESPONSE_PARAMETER_SUPPORT

    chat_cases = {
        **dict.fromkeys(("model", "messages"), "CC-01"),
        **dict.fromkeys(("max_tokens", "max_completion_tokens"), "CC-02"),
        **dict.fromkeys(("temperature", "top_p", "stop", "seed", "frequency_penalty", "presence_penalty"), "CC-03"),
        "n": "CC-04",
        "response_format": "CC-05",
        **dict.fromkeys(("tools", "tool_choice", "parallel_tool_calls"), "CC-06"),
        **dict.fromkeys(("stream", "stream_options"), "CC-07"),
        **dict.fromkeys(("user", "metadata"), "CC-08"),
        "gateway": "CC-09",
        **dict.fromkeys((
            "logprobs", "top_logprobs", "logit_bias", "functions", "function_call", "audio",
            "modalities", "prediction", "reasoning_effort", "service_tier", "store",
            "web_search_options", "verbosity", "prompt_cache_key", "prompt_cache_retention",
        ), "CC-11"),
    }
    responses_cases = {
        **dict.fromkeys(("model", "input", "instructions", "max_output_tokens", "temperature", "top_p", "metadata", "stream"), "RS-01"),
        **dict.fromkeys(("store", "previous_response_id", "input[*].id", "input[*].status", "input[*].phase"), "RS-02"),
        **dict.fromkeys((
            "text.format.type", "text.format.name", "text.format.schema", "text.format.description",
            "text.format.strict", "tools", "tool_choice", "parallel_tool_calls",
        ), "RS-03"),
        **dict.fromkeys((
            "text.verbosity", "background", "context_management", "conversation", "include", "max_tool_calls",
            "moderation", "prompt", "prompt_cache_key", "prompt_cache_options", "prompt_cache_retention",
            "reasoning", "safety_identifier", "service_tier", "stream_options",
            "stream_options.include_obfuscation", "top_logprobs", "truncation", "user",
            "input[*].type", "input[*].role", "input[*].content", "input[*].content[*].type",
            "input[*].content[*].text", "input[*].content[*].annotations", "input[*].content[*].logprobs",
        ), "RS-04"),
    }
    assert set(chat_cases) == set(STRICT_CHAT_PROFILE.parameter_support)
    assert set(responses_cases) == set(RESPONSE_PARAMETER_SUPPORT)
    # CC-10 covers nested message media parts, which are normalized by the
    # ChatMessage DTO rather than represented in STRICT_CHAT_PROFILE.
    assert set(chat_cases.values()) == {
        *(f"CC-{number:02d}" for number in range(1, 10)), "CC-11"
    }
    assert set(responses_cases.values()) == {"RS-01", "RS-02", "RS-03", "RS-04"}


@pytest.mark.asyncio
async def test_openai_sdk_models_files_and_embeddings_do_not_create_generation_samples_then_chat_does(
    latency_campaign_env,
):
    env = latency_campaign_env
    async with _sdk(env, strict_response_validation=False) as client:
        models = await client.models.list()
        assert any(model.id == "mock-chat-model" for model in models.data)
        assert (await client.models.retrieve("mock-chat-model")).id == "mock-chat-model"

        pdf_bytes = _pdf(["SDK_FILE_MARKER only in extracted input." * 8])
        uploaded = await client.files.create(
            file=("campaign.pdf", pdf_bytes, "application/pdf"), purpose="user_data"
        )
        assert (await client.files.retrieve(uploaded.id)).bytes == len(pdf_bytes)
        assert (await client.files.content(uploaded.id)).read() == pdf_bytes
        assert [item.id for item in (await client.files.list()).data] == [uploaded.id]

        embeddings = await client.embeddings.create(
            model="text-embedding-3-small", input=["first", "second"], dimensions=8,
            extra_headers={"Idempotency-Key": "latency-sdk-embeddings"},
        )
        assert len(embeddings.data) == 2
        assert len(embeddings.data[0].embedding) == 8
        assert await count_latency_observations(env["factory"]) == 0

        completion = await client.chat.completions.create(
            model="auto:fast",
            messages=[
                {"role": "system", "content": "Use all provided context."},
                {"role": "user", "content": [
                    {"type": "text", "text": "Read the attached PDF."},
                    {"type": "file", "file": {"file_id": uploaded.id}},
                ]},
                {"role": "assistant", "content": "Prior answer in history."},
                {"role": "user", "content": "What marker is present?"},
            ],
            max_completion_tokens=16,
            temperature=0.25,
            top_p=0.9,
            stop=["END"],
            seed=7,
            frequency_penalty=0.1,
            presence_penalty=0.0,
            user="sdk-user-marker",
            metadata={"case": "parameter-forwarding"},
            extra_body={"gateway": {"max_latency_ms": 4000, "allowed_providers": ["mock-openai"]}},
            extra_headers={"Idempotency-Key": "latency-sdk-file-chat"},
        )
        assert completion.choices[0].message.content == "mock upstream response"

        # Retrieval/deletion are control-plane work and do not add a provider-generation sample.
        assert (await client.files.delete(uploaded.id)).deleted

    await wait_for_campaign(lambda: _has_rows(env["factory"], 1))
    [row] = await _observations(env["factory"])
    assert row.source == "openai_sdk" and row.status == "success"
    assert row.execution_mode == "live" and row.catalog_model_id == env["model_id"]
    assert row.features_json["protocol"] == "openai_sdk"
    assert row.features_json["output_budget"] == 16
    assert "SDK_FILE_MARKER" not in json.dumps(row.features_json)
    assert "sdk-user-marker" not in json.dumps(row.features_json)
    assert "campaign.pdf" not in json.dumps(row.features_json)

    chat_call = next(call for call in env["calls"] if call["path"].endswith("/chat/completions"))
    body = chat_call["body"]
    assert body["temperature"] == 0.25 and body["top_p"] == 0.9
    assert body["stop"] == ["END"] and body["seed"] == 7
    assert body["frequency_penalty"] == 0.1 and body["presence_penalty"] == 0.0
    assert "SDK_FILE_MARKER" in json.dumps(body["messages"])
    sent_material_chars = sum(
        len(str(message.get("content", ""))) + len(str(message.get("role", "")))
        for message in body["messages"]
    )
    assert row.features_json["input_proxy"] == (sent_material_chars + 2) // 3
    # `actual_payload_chars` currently measures the routing focus string, while
    # options.messages carries the full adapter material. Preserve this evidence
    # so a later diagnostic fix can change the contract deliberately.
    assert row.actual_payload_chars is not None and row.actual_payload_chars < sent_material_chars
    assert not any(call["path"].endswith("/chat/completions") for call in env["calls"] if call is not chat_call)


@pytest.mark.asyncio
async def test_openai_sdk_schema_and_responses_state_each_record_dispatched_generations(latency_campaign_env):
    env = latency_campaign_env
    async with _sdk(env) as client:
        structured = await client.chat.completions.create(
            model="auto",
            messages=[{"role": "user", "content": "Return an answer object."}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            extra_headers={"Idempotency-Key": "latency-sdk-schema-chat"},
        )
        assert json.loads(structured.choices[0].message.content) == {"answer": "ok"}

        response = await client.responses.create(
            model="auto:fast",
            instructions="Answer briefly.",
            input="Responses first turn.",
            max_output_tokens=16,
            temperature=0.2,
            top_p=0.8,
            metadata={"case": "stored-followup"},
        )
        assert response.output_text == "mock upstream response"
        stored = await client.responses.retrieve(response.id)
        assert stored.id == response.id
        followup = await client.responses.create(
            model="auto:fast",
            previous_response_id=response.id,
            input="Responses follow-up turn.",
            store=False,
        )
        assert followup.output_text == "mock upstream response"
        await client.responses.delete(response.id)

    await wait_for_campaign(lambda: _has_rows(env["factory"], 3))
    rows = await _observations(env["factory"])
    assert len(rows) == 3
    assert all(row.source == "openai_sdk" and row.status == "success" for row in rows)
    assert [row.features_json["has_schema"] for row in rows] == [1, 0, 0]
    assert all(row.features_json["protocol"] == "openai_sdk" for row in rows)
    # Chat schema is a separate profile; plain Chat and Responses deliberately
    # share the current protocol/profile key, as recorded in the design notes.
    assert len({row.profile_key for row in rows}) == 2


@pytest.mark.asyncio
async def test_openai_sdk_inline_image_uses_ocr_surrogate_before_dispatch_and_records_generation_only(
    latency_campaign_env, monkeypatch,
):
    import asyncio

    from src.backend_ai.gateway.multimodal.ocr import OcrOutput, OcrResult
    from src.backend_ai.services import gateway_execution_service
    from src.backend_ai.services.internal_provider_clients import InternalProviderClients
    from src.shared.dtos.gateway import AttachmentKind, ModelUsage

    env = latency_campaign_env
    env["settings"].gateway_ocr_enabled = True
    env["settings"].gateway_gemini_enabled = True

    # Make the OCR provider part of the app's explicit provider allow-set while
    # pinning generation to OpenAI below. Provider/model/credential rows live
    # only in this fixture's disposable schema.
    from src.backend.dtos.catalog import ModelCreate, ProviderCreate
    from src.backend.services.catalog_service import CatalogService
    from src.shared.models.gateway import ApplicationGatewayConfig, ModelPoolMember
    from src.shared.models.provider_credentials import ProviderCredential
    from src.shared.services.credential_crypto import CredentialCrypto

    async with env["factory"]() as session:
        catalog = CatalogService(session)
        google = await catalog.create_provider(ProviderCreate(
            name="mock-google", display_name="Mock Google", kind="google",
        ))
        google_model = await catalog.create_model(ModelCreate(
            provider_id=google.id,
            name="mock-google-chat",
            display_name="Mock Google chat candidate",
            status="active",
            capabilities=["chat", "text", "long_context"],
            context_window=16_000,
            target_latency_ms=900,
        ))
        app_config = await session.scalar(select(ApplicationGatewayConfig).where(
            ApplicationGatewayConfig.application_id == env["application_id"]
        ))
        session.add(ModelPoolMember(pool_id=app_config.model_pool_id, model_id=google_model.id))
        credential_id = "latency-test-google-credential"
        crypto = CredentialCrypto.from_settings(env["settings"])
        assert crypto is not None
        session.add(ProviderCredential(
            id=credential_id,
            provider_id=google.id,
            encrypted_api_key=crypto.encrypt(
                provider_id=google.id,
                credential_id=credential_id,
                version=1,
                api_key="latency-campaign-google-key",
            ),
            version=1,
            status="active",
        ))
        await session.commit()

    class OcrSpy:
        provider = "google"
        model = "test-ocr"

        def __init__(self):
            self.seen = []

        async def ocr(self, data, mime_type, *, kind):
            self.seen.append((mime_type, kind, len(data)))
            await asyncio.sleep(0.16)
            return OcrResult(
                output=OcrOutput(
                    content_category="screenshot",
                    description="mock image description",
                    text="SDK_OCR_SURROGATE_MARKER",
                    language="en",
                ),
                model=self.model,
                usage=ModelUsage(input_tokens=4, output_tokens=2),
                usage_complete=True,
            )

    ocr = OcrSpy()

    async def fake_internal_clients(*_args, **_kwargs):
        return InternalProviderClients(ocr_client=ocr)

    monkeypatch.setattr(gateway_execution_service, "build_internal_clients", fake_internal_clients)
    image = base64.b64encode(_small_png()).decode("ascii")
    sdk = _sdk(env)
    started = time.perf_counter()
    async with sdk as client:
        completion = await client.chat.completions.create(
            model="mock-chat-model",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "Describe the image."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}", "detail": "low"}},
            ]}],
            temperature=0.1,
            max_completion_tokens=16,
            extra_body={"gateway": {"external_provider_allowed": True}},
            extra_headers={"Idempotency-Key": "latency-sdk-inline-image"},
        )
        assert completion.choices[0].message.content == "mock upstream response"
    request_wall_ms = (time.perf_counter() - started) * 1000

    await wait_for_campaign(lambda: _has_rows(env["factory"], 1))
    [row] = await _observations(env["factory"])
    assert ocr.seen and ocr.seen[0][0:2] == ("image/png", AttachmentKind.IMAGE)
    assert row.source == "openai_sdk" and row.features_json["protocol"] == "openai_sdk"
    assert request_wall_ms - row.provider_total_ms > 100
    outbound = json.dumps(env["calls"], ensure_ascii=False)
    assert "SDK_OCR_SURROGATE_MARKER" in outbound
    assert image not in outbound
    feature_dump = json.dumps(row.features_json, ensure_ascii=False)
    assert "SDK_OCR_SURROGATE_MARKER" not in feature_dump


async def _has_rows(factory, expected):
    return await count_latency_observations(factory) >= expected
