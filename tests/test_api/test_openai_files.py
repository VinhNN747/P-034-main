import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from src.shared.models.runtime import StageTraceRecord
from tests.service_app import app
from tests.test_api.test_openai_compat import openai_env  # noqa: F401 - fixture
from tests.test_gateway.test_multimodal import _pdf

PLAN_PDF = _pdf(["Ke hoach marketing Q3: ngan sach 120 trieu, kenh chinh la video ngan. " * 4] * 3)


def _sdk(key):
    from openai import AsyncOpenAI

    http_client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return AsyncOpenAI(base_url="http://test/v1", api_key=key, http_client=http_client, max_retries=0)


async def _attachment_trace(factory, request_id):
    async with factory() as session:
        return await session.scalar(
            select(StageTraceRecord).where(
                StageTraceRecord.request_id == request_id, StageTraceRecord.stage == "attachment_analysis"
            )
        )


@pytest.mark.asyncio
async def test_upload_once_then_reference_by_file_id_on_both_apis(openai_env):  # noqa: F811
    sdk = _sdk(openai_env["key"])
    uploaded = await sdk.files.create(file=("plan.pdf", PLAN_PDF, "application/pdf"), purpose="user_data")
    assert uploaded.id.startswith("file-") and uploaded.bytes == len(PLAN_PDF)
    assert uploaded.expires_at - uploaded.created_at == 24 * 60 * 60
    assert next((openai_env["tmp_path"] / "blobs").rglob(uploaded.id)).read_bytes() == PLAN_PDF

    file_part = {"type": "file", "file": {"file_id": uploaded.id}}
    completion = await sdk.chat.completions.create(
        model="auto",
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "Tom tat tai lieu"}, file_part]},
            {"role": "assistant", "content": "Tai lieu noi ve ke hoach marketing Q3."},
            {"role": "user", "content": "Ngan sach la bao nhieu?"},
        ],
    )
    trace = await _attachment_trace(openai_env["factory"], completion.id)
    assert trace.status == "success" and trace.strategy == "pdf_text_layer"
    assert trace.metadata_json["attachments"][0]["extracted_chars"] > 0

    # The same upload works through the native runtime API (messages payload).
    native = await openai_env["client"].post(
        "/api/v1/runtime/requests",
        headers={"Authorization": f"Bearer {openai_env['key']}"},
        json={"messages": [{"role": "user", "content": [{"type": "text", "text": "Doc file"}, file_part]}]},
    )
    assert native.status_code == 200, native.text
    native_trace = await _attachment_trace(openai_env["factory"], native.json()["request_id"])
    assert native_trace.strategy == "pdf_text_layer"

    listed = await sdk.files.list()
    assert [item.id for item in listed.data] == [uploaded.id]
    assert (await sdk.files.content(uploaded.id)).read() == PLAN_PDF


@pytest.mark.asyncio
async def test_files_are_isolated_per_application_and_deletable(openai_env):  # noqa: F811
    from openai import APIStatusError, NotFoundError

    owner, other = _sdk(openai_env["key"]), _sdk(openai_env["other_key"])
    uploaded = await owner.files.create(file=("plan.pdf", PLAN_PDF, "application/pdf"), purpose="user_data")
    reference = [{"role": "user", "content": [
        {"type": "text", "text": "Doc file nay"},
        {"type": "file", "file": {"file_id": uploaded.id}},
    ]}]

    with pytest.raises(NotFoundError):
        await other.files.retrieve(uploaded.id)
    with pytest.raises(NotFoundError) as error:
        await other.chat.completions.create(model="auto", messages=reference)
    assert error.value.code == "file_not_found"

    assert (await owner.files.delete(uploaded.id)).deleted
    with pytest.raises(NotFoundError):
        await owner.chat.completions.create(model="auto", messages=reference)

    with pytest.raises(APIStatusError) as unsupported:
        await owner.files.create(file=("x.zip", b"PK\x03\x04" + b"\x00" * 20, "application/zip"), purpose="user_data")
    assert unsupported.value.status_code == 400
    with pytest.raises(APIStatusError) as too_large:
        await owner.files.create(file=("big.pdf", PLAN_PDF * 200, "application/pdf"), purpose="user_data")
    assert too_large.value.status_code == 413


@pytest.mark.asyncio
async def test_expires_after_is_honoured_within_bounds(openai_env):  # noqa: F811
    from openai import BadRequestError

    sdk = _sdk(openai_env["key"])
    two_hours = await sdk.files.create(
        file=("plan.pdf", PLAN_PDF, "application/pdf"),
        purpose="user_data",
        expires_after={"anchor": "created_at", "seconds": 7200},
    )
    assert two_hours.expires_at - two_hours.created_at == 7200
    with pytest.raises(BadRequestError):
        await sdk.files.create(
            file=("plan.pdf", PLAN_PDF, "application/pdf"),
            purpose="user_data",
            expires_after={"anchor": "created_at", "seconds": 60},
        )


@pytest.mark.asyncio
async def test_sdk_files_auto_iteration_has_no_gaps(openai_env):  # noqa: F811
    sdk = _sdk(openai_env["key"])
    created = [
        await sdk.files.create(
            file=(f"page-{index}.txt", f"content {index}".encode(), "text/plain"), purpose="user_data"
        )
        for index in range(3)
    ]

    listed = [item async for item in sdk.files.list(limit=1)]

    assert len(listed) == 3
    assert {item.id for item in listed} == {item.id for item in created}
    assert len({item.id for item in listed}) == len(listed)


@pytest.mark.asyncio
async def test_files_after_order_purpose_and_limits(openai_env):  # noqa: F811
    client, key = openai_env["client"], openai_env["key"]
    sdk = _sdk(key)
    records = []
    for filename, purpose in (("one.txt", "user_data"), ("two.txt", "vision"), ("three.txt", "user_data")):
        records.append(
            await sdk.files.create(
                file=(filename, filename.encode(), "text/plain"), purpose=purpose
            )
        )
    headers = {"Authorization": f"Bearer {key}"}

    async def get_page(**params):
        response = await client.get("/v1/files", params=params, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    asc_first = await get_page(limit=1, order="asc")
    assert len(asc_first["data"]) == 1 and asc_first["has_more"] is True
    asc_second = await get_page(limit=1, order="asc", after=asc_first["last_id"])
    assert len(asc_second["data"]) == 1

    desc = await get_page(limit=10000, order="desc")
    asc = await get_page(limit=10000, order="asc")
    assert {item["id"] for item in desc["data"]} == {item.id for item in records}
    assert [item["id"] for item in desc["data"]] == list(reversed([item["id"] for item in asc["data"]]))
    assert desc["has_more"] is False
    assert desc["first_id"] == desc["data"][0]["id"]
    assert desc["last_id"] == desc["data"][-1]["id"]

    vision = await get_page(limit=1, purpose="vision")
    assert [item["purpose"] for item in vision["data"]] == ["vision"]
    empty = await get_page(limit=1, purpose="unknown-purpose")
    assert empty["data"] == [] and empty["has_more"] is False
    assert empty["first_id"] is None and empty["last_id"] is None

    for query, param in (({"limit": 0}, "limit"), ({"limit": 10001}, "limit"), ({"limit": "abc"}, "limit"), ({"order": "random"}, "order")):
        response = await client.get("/v1/files", params=query, headers=headers)
        assert response.status_code == 400
        assert response.json()["error"]["param"] == param


@pytest.mark.asyncio
async def test_file_cursor_is_application_scoped(openai_env):  # noqa: F811
    other = _sdk(openai_env["other_key"])
    foreign = await other.files.create(
        file=("private.txt", b"private filename", "text/plain"), purpose="user_data"
    )
    headers = {"Authorization": f"Bearer {openai_env['key']}"}
    responses = [
        await openai_env["client"].get("/v1/files", params={"after": cursor}, headers=headers)
        for cursor in (foreign.id, "file-not-a-cursor")
    ]

    errors = [response.json()["error"] for response in responses]
    assert all(response.status_code == 400 for response in responses)
    assert errors[0]["code"] == errors[1]["code"] == "invalid_cursor"
    assert errors[0]["message"] == errors[1]["message"]
    assert errors[0]["param"] == errors[1]["param"] == "after"
    assert "private filename" not in str(errors)


@pytest.mark.asyncio
async def test_file_cursor_with_changed_purpose(openai_env):  # noqa: F811
    from sqlalchemy import select

    from src.shared.models.files import GatewayFileRecord

    sdk = _sdk(openai_env["key"])
    await sdk.files.create(file=("one.txt", b"one", "text/plain"), purpose="user_data")
    await sdk.files.create(file=("two.txt", b"two", "text/plain"), purpose="vision")
    await sdk.files.create(file=("three.txt", b"three", "text/plain"), purpose="vision")
    async with openai_env["factory"]() as session:
        ordered = list(
            await session.scalars(
                select(GatewayFileRecord).order_by(GatewayFileRecord.created_at, GatewayFileRecord.id)
            )
        )
    cursor = ordered[0].id
    cursor_position = (ordered[0].created_at, cursor)
    expected = [
        record.id
        for record in ordered
        if (record.created_at, record.id) > cursor_position and record.purpose == "vision"
    ]

    response = await openai_env["client"].get(
        "/v1/files",
        params={"after": cursor, "order": "asc", "limit": 10, "purpose": "vision"},
        headers={"Authorization": f"Bearer {openai_env['key']}"},
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["data"]] == expected
