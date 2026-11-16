import pytest
from sqlalchemy import func, select

from src.shared.models.runtime import GatewayRequestRecord, GatewayTaskRecord, StageTraceRecord
from tests.test_api.test_openai_compat import MULTI, _auth, _chat, openai_env  # noqa: F401


@pytest.mark.asyncio
async def test_probe(openai_env):  # noqa: F811 - pytest fixture argument
    client, key = openai_env["client"], openai_env["key"]
    for i in range(25):
        await client.post("/v1/chat/completions", json=_chat(MULTI + f" #{i}"), headers=_auth(key))
        r = await client.post("/v1/chat/completions", json=_chat(MULTI + f" #{i}", model="auto:fast"), headers=_auth(key))
        rid = r.json()["id"]
        async with openai_env["factory"]() as s:
            _trace = await s.scalar(select(StageTraceRecord).where(StageTraceRecord.request_id == rid, StageTraceRecord.stage == "decomposition_decision"))
            d = await s.scalar(select(StageTraceRecord).where(StageTraceRecord.request_id == rid, StageTraceRecord.stage == "decompose"))
            _request = await s.get(GatewayRequestRecord, rid)
            v = await s.scalar(select(StageTraceRecord).where(StageTraceRecord.request_id == rid, StageTraceRecord.stage == "decomposition_validation"))
            n = await s.scalar(select(func.count()).select_from(GatewayTaskRecord).where(GatewayTaskRecord.request_id == rid))
            print(i, "tasks", n, "decompose", d.strategy, "validation", v.status, v.metadata_json.get("errors"))
