"""Functional audit with real providers and isolated schema/storage.

Copies only API-authorized configuration for the two supplied applications.
Production migrations are a separate release review gate. Secrets use stdin.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import termios
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from openai import APIStatusError, OpenAI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.scripts.openai_audit_session import AuditSession, evidence_context
from src.shared.models.billing import BillingAccountRecord, BillingTransactionRecord
from src.shared.models.core import Application
from src.shared.models.gateway import ApplicationGatewayConfig, Model, ModelPool, ModelPoolMember, Provider
from src.shared.models.integration import ApplicationApiKeyRecord
from src.shared.models.runtime import RuntimeOperationRecord, StageTraceRecord, UsageEventRecord

FIXTURE_URL = "postgresql+psycopg://postgres:fixture-password@127.0.0.1:15434/gateway_audit"
AI_ROOT = "http://127.0.0.1:18002"


def credentials_from_stdin():
    original = termios.tcgetattr(sys.stdin.fileno())
    quiet = original.copy()
    quiet[3] &= ~termios.ECHO
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, quiet)
    print("Credential input ready", flush=True)
    try:
        line = sys.stdin.readline()
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, original)
    return json.loads(line)


async def seed_stage(apps, configs, models, providers, keys):
    engine = create_async_engine(FIXTURE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            chosen = [model for model in models if model["name"] in {"gpt-4o-mini", "text-embedding-3-small"}]
            assert {model["name"] for model in chosen} == {"gpt-4o-mini", "text-embedding-3-small"}
            for provider in providers:
                if (
                    provider["id"] in {model["provider_id"] for model in chosen}
                    and await session.get(Provider, provider["id"]) is None
                ):
                    session.add(
                        Provider(
                            **{
                                key: provider[key]
                                for key in (
                                    "id",
                                    "name",
                                    "display_name",
                                    "kind",
                                    "status",
                                    "base_url",
                                )
                            }
                        )
                    )
            await session.flush()
            for model in chosen:
                if await session.get(Model, model["id"]) is None:
                    session.add(
                        Model(
                            id=model["id"],
                            provider_id=model["provider_id"],
                            name=model["name"],
                            display_name=model["display_name"],
                            status=model["status"],
                            capabilities_json=model["capabilities"],
                            context_window=model["context_window"],
                            quality_tier=model["quality_tier"],
                            input_price_per_million=model["pricing"]["input_per_million"],
                            output_price_per_million=model["pricing"]["output_per_million"],
                            pricing_version=model["pricing"]["version"],
                        )
                    )
            await session.flush()
            for label, application in apps.items():
                if await session.get(Application, application["id"]) is not None:
                    continue
                session.add(
                    Application(
                        id=application["id"],
                        owner_user_id=application["owner_user_id"],
                        name="SDK isolated " + label,
                        slug="sdk-isolated-" + label,
                        environment="development",
                    )
                )
                await session.flush()
                pool = ModelPool(
                    owner_user_id=application["owner_user_id"], name="sdk-isolated-" + label, purpose="runtime"
                )
                session.add(pool)
                await session.flush()
                session.add_all(ModelPoolMember(pool_id=pool.id, model_id=model["id"]) for model in chosen)
                config = configs[label]
                accepted = {column.key for column in ApplicationGatewayConfig.__table__.columns} - {
                    "id",
                    "application_id",
                    "model_pool_id",
                    "routing_policy_id",
                    "created_at",
                    "updated_at",
                }
                values = {key: value for key, value in config.items() if key in accepted}
                values["fallback_model_ids_json"] = []
                values["fallback_model_id"] = None
                session.add(ApplicationGatewayConfig(application_id=application["id"], model_pool_id=pool.id, **values))
                session.add(
                    ApplicationApiKeyRecord(
                        application_id=application["id"],
                        name="sdk-isolated-original-key",
                        key_prefix=keys[label][:12],
                        key_hash=hashlib.sha256(keys[label].encode()).hexdigest(),
                    )
                )
                if (
                    await session.scalar(
                        select(BillingAccountRecord).where(BillingAccountRecord.user_id == application["owner_user_id"])
                    )
                    is None
                ):
                    session.add(BillingAccountRecord(user_id=application["owner_user_id"], balance=Decimal("100")))
                await session.flush()
            await session.commit()
    finally:
        await engine.dispose()


async def accounting(application_id, request_id, receipt):
    engine = create_async_engine(FIXTURE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            operations = await session.scalar(
                select(func.count())
                .select_from(RuntimeOperationRecord)
                .where(
                    RuntimeOperationRecord.application_id == application_id,
                    RuntimeOperationRecord.request_id == request_id,
                )
            )
            debits = list(
                await session.scalars(
                    select(BillingTransactionRecord).where(BillingTransactionRecord.source_request_id == request_id)
                )
            )
            usage = list(
                await session.scalars(select(UsageEventRecord).where(UsageEventRecord.request_id == request_id))
            )
            cost = Decimal(str(receipt["cost"]["estimated_cost"]))
            assert operations == 1 and len(debits) == 1 and len(usage) == 1
            assert abs(-debits[0].amount - cost) <= Decimal("0.000001")
            assert abs(Decimal(str(usage[0].cost_usd)) - cost) <= Decimal("0.000001")
            return {"request_id": request_id, "operations": operations, "debits": len(debits), "cost_usd": float(cost)}
    finally:
        await engine.dispose()


async def attachment_evidence(application_id, request_id):
    engine = create_async_engine(FIXTURE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            trace = await session.scalar(select(StageTraceRecord).join(RuntimeOperationRecord,
                RuntimeOperationRecord.request_id == StageTraceRecord.request_id).where(
                RuntimeOperationRecord.application_id == application_id, StageTraceRecord.request_id == request_id,
                StageTraceRecord.stage == 'attachment_analysis'))
            assert trace is not None and trace.status == 'success'
            meta = trace.metadata_json
            assert meta['analyzed_count'] == meta['attachment_count'] == 1
            assert sum(item['extracted_chars'] for item in meta['attachments']) > 0
            return {'attachment_count': meta['attachment_count'], 'analyzed_count': meta['analyzed_count']}
    finally:
        await engine.dispose()


def main():
    credentials = credentials_from_stdin()
    cfg = dotenv_values(".env")
    run_id = uuid4().hex
    report = {
        "run_id": run_id,
        "evidence_kind": "isolated-postgresql16-real-provider",
        "production_schema_modified": False,
        "sdk_python": "2.54.0",
        "cases": [],
        "cleanup": [],
        "revision": evidence_context(),
    }
    keys = credentials["keys"]
    with AuditSession(
        cfg["SUPABASE_URL"], cfg["SUPABASE_PUBLISHABLE_KEY"], "http://127.0.0.1:18000/api/v1"
    ) as management:
        management.login(credentials["email"], credentials["password"])
        apps, configs = {}, {}
        for label, key in keys.items():
            aid = management.resolve_application(label, key)
            apps[label] = management.management("GET", "/applications/" + aid).json()
            configs[label] = management.management("GET", "/applications/" + aid + "/gateway-config").json()
            authenticated = httpx.get(
                "http://127.0.0.1:18001/v1/models", headers={"Authorization": "Bearer " + key}, timeout=30
            )
            assert authenticated.status_code == 200
        models = management._items("/gateway/available-models")
        providers = management.management("GET", "/gateway/providers").json()
        asyncio.run(seed_stage(apps, configs, models, providers, keys))
        clients = {
            label: OpenAI(
                base_url=AI_ROOT + "/v1", api_key=key, timeout=60, max_retries=0, _strict_response_validation=True
            )
            for label, key in keys.items()
        }
        raw = httpx.Client(timeout=60)
        files, response_ids = [], []
        request_ids = {}

        def case(label, name, action):
            started = time.monotonic()
            try:
                result = action() or {}
                report["cases"].append(
                    {
                        "app": label,
                        "case_id": name,
                        "status": "pass",
                        **result,
                        "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    }
                )
            except Exception as exc:
                report["cases"].append(
                    {
                        "app": label,
                        "case_id": name,
                        "status": "fail",
                        "error_class": type(exc).__name__,
                        "http_status": getattr(exc, "status_code", None),
                        "error_code": getattr(exc, "code", None),
                        "request_id": getattr(exc, "request_id", None),
                    }
                )
            print(json.dumps(report["cases"][-1]), flush=True)

        def operation(name):
            return {"Idempotency-Key": "stage-" + run_id[:12] + "-" + name}

        def receipt(label, request_id):
            value = raw.get(
                AI_ROOT + "/api/v1/runtime/requests/" + request_id, headers={"Authorization": "Bearer " + keys[label]}
            )
            assert value.status_code == 200
            return value.json()

        try:
            for label, client in clients.items():
                other_label = next(other for other in keys if other != label)
                other = clients[other_label]

                def discovery():
                    cards = client.models.list().data
                    concrete = client.models.retrieve("gpt-4o-mini")
                    assert concrete.created > 0 and any(card.id == concrete.id for card in cards)
                    return {"models": len(cards)}

                case(label, "models-list-retrieve", discovery)

                def chat():
                    args = dict(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": "Reply with one short greeting."}],
                        max_completion_tokens=48,
                        extra_headers=operation("chat"),
                    )
                    first = client.chat.completions.create(**args)
                    replay = client.chat.completions.create(**args)
                    assert first.model_dump() == replay.model_dump() and first.usage.completion_tokens <= 48
                    assert first.choices[0].finish_reason in {"stop", "length", "content_filter"}
                    request_ids[label] = first.id
                    r = receipt(label, first.id)
                    assert first.usage.total_tokens == r["usage"]["final_output"]["total_tokens"]
                    result = asyncio.run(accounting(apps[label]["id"], first.id, r))
                    result.update(
                        replay=True,
                        output_tokens=first.usage.completion_tokens,
                        finish_reason=first.choices[0].finish_reason,
                    )
                    return result

                case(label, "chat-cap-replay-accounting", chat)

                def isolation():
                    own = raw.get(
                        AI_ROOT + "/api/v1/runtime/requests/" + request_ids[label],
                        headers={"Authorization": "Bearer " + keys[other_label]},
                    )
                    assert own.status_code == 404
                    assert request_ids.get(other_label) != request_ids[label]
                    return {"request_id": request_ids[label]}

                case(label, "receipt-and-same-key-application-isolation", isolation)

                def sampling():
                    result = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": "Say hello."}],
                        temperature=0,
                        top_p=0.5,
                        seed=42,
                        frequency_penalty=0,
                        presence_penalty=0,
                        stop=["END"],
                        max_completion_tokens=48,
                        extra_headers=operation("sampling"),
                    )
                    assert result.usage.completion_tokens <= 48
                    return {"request_id": result.id, "output_tokens": result.usage.completion_tokens}

                case(label, "sampling-options", sampling)

                def schema():
                    from pydantic import BaseModel

                    class Answer(BaseModel):
                        answer: str

                    result = client.chat.completions.parse(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": "Answer with a greeting."}],
                        response_format=Answer,
                        max_completion_tokens=64,
                        extra_headers=operation("schema"),
                    )
                    assert isinstance(result.choices[0].message.parsed, Answer)
                    return {"request_id": result.id, "schema_validated": True}

                case(label, "native-schema-sdk-parse", schema)

                def tools():
                    tool = {
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "description": "Look up weather for a city",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                                "additionalProperties": False,
                            },
                            "strict": True,
                        },
                    }
                    history = [{"role": "user", "content": "What is the weather in Paris?"}]
                    result = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=history,
                        tools=[tool],
                        tool_choice={"type": "function", "function": {"name": "weather"}},
                        max_completion_tokens=128,
                        extra_headers=operation("tools"),
                    )
                    calls = result.choices[0].message.tool_calls
                    assert result.choices[0].finish_reason == "tool_calls" and calls
                    assert all(
                        call.function.name == "weather" and isinstance(json.loads(call.function.arguments), dict)
                        for call in calls
                    )
                    history.append(result.choices[0].message.model_dump(exclude_none=True))
                    history.extend(
                        {"role": "tool", "tool_call_id": call.id, "content": "Sunny, 20 C"} for call in calls
                    )
                    followup = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=history,
                        tools=[tool],
                        tool_choice="none",
                        max_completion_tokens=64,
                        extra_headers=operation("tools-result"),
                    )
                    assert followup.choices[0].message.content
                    return {"request_id": result.id, "followup_request_id": followup.id, "tool_ids_preserved": True}

                case(label, "function-call-roundtrip", tools)

                def choices():
                    result = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": "Say hello."}],
                        n=2,
                        max_completion_tokens=24,
                        extra_headers=operation("choices"),
                    )
                    assert [choice.index for choice in result.choices] == [
                        0,
                        1,
                    ] and result.usage.completion_tokens <= 48
                    return {"request_id": result.id, "choice_indexes": [choice.index for choice in result.choices]}

                case(label, "multiple-choices", choices)

                def chat_stream():
                    started = time.monotonic()
                    deltas = []
                    first_ms = None
                    for chunk in client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": "Write three short numbered sentences."}],
                        stream=True,
                        stream_options={"include_usage": True},
                        max_completion_tokens=96,
                        extra_headers=operation("chat-stream"),
                    ):
                        if chunk.choices and chunk.choices[0].delta.content:
                            if first_ms is None:
                                first_ms = (time.monotonic() - started) * 1000
                            deltas.append(chunk.choices[0].delta.content)
                        last = chunk
                    assert deltas and last.usage and last.usage.completion_tokens <= 96
                    total = (time.monotonic() - started) * 1000
                    assert first_ms < total
                    return {
                        "request_id": last.id,
                        "content_delta_count": len(deltas),
                        "ttft_ms": round(first_ms, 2),
                        "completion_ms": round(total, 2),
                    }

                case(label, "chat-real-stream", chat_stream)

                def responses():
                    body = dict(
                        model="gpt-4o-mini",
                        input="Say a short greeting.",
                        max_output_tokens=48,
                        extra_headers=operation("response-store"),
                    )
                    result = client.responses.create(**body)
                    response_ids.append((client, result.id))
                    assert result.store and result.output_text
                    assert client.responses.retrieve(result.id).model_dump() == result.model_dump()
                    try:
                        other.responses.retrieve(result.id)
                    except APIStatusError as exc:
                        assert exc.status_code == 404
                    else:
                        raise AssertionError("Cross-application response access")
                    next_result = client.responses.create(
                        model="gpt-4o-mini",
                        input="Say goodbye.",
                        previous_response_id=result.id,
                        max_output_tokens=48,
                        extra_headers=operation("response-next"),
                    )
                    response_ids.append((client, next_result.id))
                    assert next_result.previous_response_id == result.id and next_result.output_text
                    return {
                        "response_id": result.id,
                        "previous_response_id": next_result.previous_response_id,
                        "gateway_usage": result.gateway_usage,
                    }

                case(label, "responses-default-storage-history-isolation", responses)

                def response_stream():
                    with client.responses.stream(
                        model="gpt-4o-mini",
                        input="Write three short sentences.",
                        max_output_tokens=96,
                        extra_headers=operation("response-stream"),
                    ) as stream:
                        events = list(stream)
                        final = stream.get_final_response()
                    response_ids.append((client, final.id))
                    assert any(event.type == "response.output_text.delta" for event in events) and final.output_text
                    assert [e.sequence_number for e in events] == list(range(len(events)))
                    return {"response_id": final.id, "event_count": len(events), "response_status": final.status}

                case(label, "responses-real-stream-helper", response_stream)

                def file_roundtrip():
                    file = client.files.create(
                        file=("audit.txt", b"Untrusted document: greeting is hello.", "text/plain"), purpose="user_data"
                    )
                    files.append((client, file.id))
                    assert client.files.retrieve(file.id).bytes == file.bytes
                    assert client.files.content(file.id).read() == b"Untrusted document: greeting is hello."
                    assert any(item.id == file.id for item in client.files.list(limit=1))
                    try:
                        other.files.content(file.id)
                    except APIStatusError as exc:
                        assert exc.status_code == 404
                    else:
                        raise AssertionError("Cross-application file access")
                    result = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "What greeting does the document contain?"},
                                    {"type": "file", "file": {"file_id": file.id}},
                                ],
                            }
                        ],
                        max_completion_tokens=48,
                        extra_headers=operation("file-reference"),
                    )
                    assert result.choices[0].message.content
                    analysis = asyncio.run(attachment_evidence(apps[label]['id'], result.id))
                    return {"file_id": file.id, "request_id": result.id, "bytes": file.bytes,
                            "two_way_isolation": True, **analysis}

                case(label, "files-all-routes-reference-isolation", file_roundtrip)
        finally:
            for client, file_id in files:
                try:
                    assert client.files.delete(file_id).deleted
                except Exception:
                    report["cleanup"].append({"type": "file", "id": file_id, "status": "fail"})
                else:
                    report["cleanup"].append({"type": "file", "id": file_id, "status": "pass"})
            for client, response_id in response_ids:
                try:
                    # The pinned SDK deliberately returns None for DELETE.
                    deletion = client.responses.with_raw_response.delete(response_id)
                    assert deletion.status_code == 200
                    assert deletion.http_response.json()["deleted"] is True
                    try:
                        client.responses.retrieve(response_id)
                    except APIStatusError as exc:
                        assert exc.status_code == 404
                    else:
                        raise AssertionError("Deleted response remains readable")
                except Exception:
                    report["cleanup"].append({"type": "response", "id": response_id, "status": "fail"})
                else:
                    report["cleanup"].append({"type": "response", "id": response_id, "status": "pass"})
            for client in clients.values():
                client.close()
            raw.close()
    Path("docs/openai-sdk/evidence/2026-10-09-staging-real-provider.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(report["cases"]),
                "failures": sum(case["status"] == "fail" for case in report["cases"]),
                "cleanup": report["cleanup"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"setup_status": "fail", "error_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
