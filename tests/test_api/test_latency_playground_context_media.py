from __future__ import annotations

import base64
import json
import struct
import zlib

import pytest
from sqlalchemy import select

from src.backend_ai.gateway.multimodal.ocr import OcrOutput, OcrResult
from src.backend_ai.services.internal_provider_clients import InternalProviderClients
from src.shared.dtos.gateway import AttachmentKind, ModelUsage
from src.shared.models.latency import LatencyObservationRecord
from tests.latency_support import count_latency_observations, wait_for_campaign
from tests.test_gateway.test_multimodal import _pdf

pytest_plugins = ("tests.latency_support",)


def _chunk(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload))


def _png_1x1() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    image_data = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", image_data) + _chunk(b"IEND", b"")


JPEG_16PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYwLjMxLjEwMgD/2wBDAAgKCgsKCw0NDQ0NDRAPEBAQEBAQEBASEhIVFRUSEhIQEBISFBQVFRUXFxUVFRUXFxkZGR4eHBwjIyQrKzP/xABMAAEBAAAAAAAAAAAAAAAAAAAABgEBAQAAAAAAAAAAAAAAAAAABgcQAQAAAAAAAAAAAAAAAAAAAAARAQAAAAAAAAAAAAAAAAAAAAD/wAARCAAQABADASIAAhEAAxEA/9oADAMBAAIRAxEAPwCLAFF/f//Z"
)


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _playground_body(env, messages, *, context=None):
    return {
        "application_id": env["application_id"],
        "messages": messages,
        "context": context or {},
        "source": "playground",
        "understander": "rule",
        "decomposer": "deterministic",
        "router": "database",
        "aggregator": "deterministic",
    }


async def _observation_rows(env):
    async with env["factory"]() as session:
        return list((await session.scalars(
            select(LatencyObservationRecord).order_by(LatencyObservationRecord.dispatched_at)
        )).all())


async def _has_rows(env, expected: int) -> bool:
    return await count_latency_observations(env["factory"]) >= expected


@pytest.mark.asyncio
async def test_playground_context_roles_and_file_id_reach_generation_without_raw_telemetry(
    latency_campaign_env,
):
    env = latency_campaign_env
    client = env["client"]
    pdf_text = "PDF_CONTEXT_MARKER: contract budget 120 million VND. " * 8
    pdf_bytes = _pdf([pdf_text])

    uploaded = await client.post(
        "/v1/files",
        headers={"Authorization": f"Bearer {env['key']}"},
        data={"purpose": "user_data"},
        files={"file": ("context.pdf", pdf_bytes, "application/pdf")},
    )
    assert uploaded.status_code == 200, uploaded.text
    file_id = uploaded.json()["id"]

    response = await client.post(
        "/api/v1/gateway/requests",
        json=_playground_body(
            env,
            [
                {"role": "system", "content": "SYSTEM_ROLE_MARKER"},
                {"role": "user", "content": "HISTORY_ROLE_MARKER"},
                {"role": "assistant", "content": "ASSISTANT_HISTORY_MARKER"},
                {"role": "user", "content": [
                    {"type": "text", "text": "FOCUS_ROLE_MARKER: summarize this PDF"},
                    {"type": "file", "file": {"file_id": file_id}},
                ]},
            ],
            context={"case_id": "REQUEST_CONTEXT_MARKER"},
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    await wait_for_campaign(lambda: _has_rows(env, 1))

    material = json.dumps(env["calls"], ensure_ascii=False)
    for marker in (
        "SYSTEM_ROLE_MARKER",
        "HISTORY_ROLE_MARKER",
        "ASSISTANT_HISTORY_MARKER",
        "FOCUS_ROLE_MARKER",
        "PDF_CONTEXT_MARKER",
        "REQUEST_CONTEXT_MARKER",
    ):
        assert marker in material

    [observation] = await _observation_rows(env)
    assert observation.source == "playground"
    assert observation.execution_mode == "live"
    assert observation.status == "success"
    feature_dump = json.dumps(observation.features_json, ensure_ascii=False)
    assert all(marker not in feature_dump for marker in (
        "SYSTEM_ROLE_MARKER", "HISTORY_ROLE_MARKER", "PDF_CONTEXT_MARKER", "REQUEST_CONTEXT_MARKER",
    ))
    assert "context.pdf" not in feature_dump
    assert base64.b64encode(pdf_bytes).decode("ascii") not in feature_dump

    foreign_request = _playground_body(
        env,
        [{"role": "user", "content": [{"type": "file", "file": {"file_id": file_id}}]}],
    )
    foreign_request["application_id"] = env["other_application_id"]
    other = await client.post("/api/v1/gateway/requests", json=foreign_request)
    assert other.status_code == 404
    assert await count_latency_observations(env["factory"]) == 1


@pytest.mark.asyncio
async def test_image_and_scanned_pdf_use_ocr_surrogate_and_keep_ocr_out_of_latency_label(
    latency_campaign_env, monkeypatch,
):
    import asyncio

    from src.backend_ai.services import gateway_execution_service

    env = latency_campaign_env
    env["settings"].gateway_ocr_enabled = True

    class DelayedOcr:
        provider = "google"
        model = "fake-ocr-model"

        def __init__(self):
            self.calls = []

        async def ocr(self, data, mime_type, *, kind):
            self.calls.append((mime_type, kind, len(data)))
            await asyncio.sleep(0.18)
            return OcrResult(
                output=OcrOutput(
                    content_category="document" if kind == AttachmentKind.PDF else "screenshot",
                    description="Mock OCR result",
                    text="OCR_SURROGATE_MARKER",
                    language="en",
                ),
                model=self.model,
                usage=ModelUsage(input_tokens=12, output_tokens=4),
                usage_complete=True,
            )

    ocr = DelayedOcr()

    async def fake_internal_clients(*_args, **_kwargs):
        return InternalProviderClients(ocr_client=ocr)

    monkeypatch.setattr(gateway_execution_service, "build_internal_clients", fake_internal_clients)

    png = _png_1x1()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert JPEG_16PX.startswith(b"\xff\xd8\xff")
    scanned_pdf = _pdf([""])
    attachments = [
        ("image.png", "image/png", _data_url(png, "image/png")),
        ("image.jpg", "image/jpeg", _data_url(JPEG_16PX, "image/jpeg")),
        ("scan.pdf", "application/pdf", _data_url(scanned_pdf, "application/pdf")),
    ]

    returned_latencies = []
    for filename, mime, data_url in attachments:
        if mime == "application/pdf":
            part = {"type": "file", "file": {"filename": filename, "file_data": data_url}}
        else:
            part = {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}}
        response = await env["client"].post(
            "/api/v1/gateway/requests",
            json=_playground_body(env, [{"role": "user", "content": [
                {"type": "text", "text": f"Describe {filename}"}, part,
            ]}]),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        returned_latencies.append(response.json()["latency_ms"])

    await wait_for_campaign(lambda: _has_rows(env, 3))
    assert len(ocr.calls) == 3
    assert {mime for mime, _, _ in ocr.calls} == {"image/png", "image/jpeg", "application/pdf"}
    assert all(kind in {AttachmentKind.IMAGE, AttachmentKind.PDF} for _, kind, _ in ocr.calls)
    assert all(elapsed_ms >= 150 for elapsed_ms in returned_latencies)

    rows = await _observation_rows(env)
    assert len(rows) == 3
    assert all(row.status == "success" for row in rows)
    assert all(
        row.provider_total_ms > 0 and request_ms - row.provider_total_ms > 120
        for row, request_ms in zip(rows, returned_latencies, strict=True)
    )
    material = json.dumps(env["calls"], ensure_ascii=False)
    assert material.count("OCR_SURROGATE_MARKER") == 3
    for row in rows:
        feature_dump = json.dumps(row.features_json, ensure_ascii=False)
        assert "OCR_SURROGATE_MARKER" not in feature_dump
        assert all(filename not in feature_dump for filename, _, _ in attachments)
