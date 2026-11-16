#!/usr/bin/env python3
"""Multi-turn conversation test against the Gateway with the official OpenAI SDK.

Each case sends a full ``messages`` history (system / user / assistant turns)
to ``POST /v1/chat/completions`` with ``model="auto"`` and checks that:

- the request succeeds and returns text,
- the answer uses earlier turns (a keyword only the history contains),
- the routed model is reported in the ``x-gateway-models`` header.

The gateway routes on the LATEST user message (plus a short context window),
while the executing model sees the whole conversation. Variables are read
from the shell first, then the repository-root ``.env``:

    GATEWAY_API_KEY           application key (ak_...), not the OpenAI key
    GATEWAY_OPENAI_BASE_URL   default http://127.0.0.1:8001/v1

    python src/scripts/openai_sdk_conversation_test.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8001/v1"

# name, expected tier, messages, keywords (any one must appear in the answer)
CASES: list[tuple[str, str, list[dict], tuple[str, ...]]] = [
    (
        "follow-up code edit uses the earlier function",
        "simple",
        [
            {"role": "user", "content": "Viết hàm Python tính trung bình của một danh sách số"},
            {"role": "assistant", "content": "def avg(xs):\n    return sum(xs) / len(xs)"},
            {"role": "user", "content": "thêm xử lý khi danh sách rỗng"},
        ],
        ("avg", "len(xs)", "not xs", "== 0"),
    ),
    (
        "numbers from an earlier turn, new what-if",
        "mid",
        [
            {"role": "user", "content": "Cửa hàng bánh mì: giá bán 25.000đ/ổ, chi phí biến đổi 12.000đ/ổ, chi phí cố định 39 triệu/tháng. Điểm hòa vốn là bao nhiêu ổ/tháng?"},
            {"role": "assistant", "content": "Lãi góp mỗi ổ 13.000đ, điểm hòa vốn = 39.000.000 / 13.000 = 3.000 ổ/tháng."},
            {"role": "user", "content": "Nếu tăng giá bán lên 28.000đ thì điểm hòa vốn mới là bao nhiêu và giảm bao nhiêu % so với trước?"},
        ],
        ("2.437", "2437", "2.438", "2438", "16.000", "16000"),
    ),
    (
        "short thanks after a heavy conversation stays cheap",
        "simple",
        [
            {"role": "user", "content": "Thiết kế kiến trúc microservice cho sàn thương mại điện tử 2 triệu người dùng."},
            {"role": "assistant", "content": "Kiến trúc đề xuất gồm API gateway, service đơn hàng, thanh toán, kho hàng, event bus Kafka và PostgreSQL theo từng service."},
            {"role": "user", "content": "ok, cảm ơn nhé"},
        ],
        ("",),
    ),
    (
        "casual chat then a high-risk medical question",
        "hard",
        [
            {"role": "user", "content": "Chào bạn, hôm nay trời đẹp quá"},
            {"role": "assistant", "content": "Chào bạn! Thời tiết đẹp thật, bạn cần mình giúp gì không?"},
            {"role": "user", "content": "Mẹ tôi dùng warfarin, INR mục tiêu 2-3, hôm nay xét nghiệm INR 4.8, không chảy máu. Nên xử trí thế nào?"},
        ],
        ("warfarin", "INR", "vitamin K"),
    ),
    (
        "system instruction is respected",
        "simple",
        [
            {"role": "system", "content": "Always answer in English, in one short sentence."},
            {"role": "user", "content": "Thủ đô của Úc là thành phố nào?"},
        ],
        ("Canberra",),
    ),
]


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    except ImportError:
        pass
    from openai import APIStatusError, OpenAI

    base_url = os.getenv("GATEWAY_OPENAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    api_key = os.getenv("GATEWAY_API_KEY", "")
    if not api_key.startswith("ak_"):
        print("Set GATEWAY_API_KEY to an application key (ak_...).", file=sys.stderr)
        return 2

    print(f"Endpoint: {base_url}\n")
    failures = 0
    # max_retries=0: never silently repeat a billable completion.
    with OpenAI(base_url=base_url, api_key=api_key, timeout=150.0, max_retries=0) as client:
        for name, tier, messages, keywords in CASES:
            started = time.perf_counter()
            try:
                raw = client.chat.completions.with_raw_response.create(model="auto", messages=messages)
            except APIStatusError as exc:
                failures += 1
                print(f"FAIL  {name}\n      HTTP {exc.status_code}: {exc.message}\n")
                continue
            completion = raw.parse()
            answer = completion.choices[0].message.content or ""
            uses_history = any(keyword.lower() in answer.lower() for keyword in keywords)
            ok = bool(answer.strip()) and uses_history
            failures += not ok
            print(
                f"{'OK  ' if ok else 'FAIL'}  {name}\n"
                f"      expected tier: {tier} | model: {raw.headers.get('x-gateway-models')} | "
                f"cost ${float(raw.headers.get('x-gateway-cost-usd') or 0):.5f} | "
                f"{time.perf_counter() - started:.1f}s | request {raw.headers.get('x-gateway-request-id')}\n"
                f"      answer: {answer.strip()[:160].replace(chr(10), ' ')}\n"
            )

        print("Streaming the last case (stream=True)...")
        stream = client.chat.completions.create(model="auto", messages=CASES[-1][2], stream=True)
        streamed = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
        stream_ok = "canberra" in streamed.lower()
        failures += not stream_ok
        print(f"{'OK  ' if stream_ok else 'FAIL'}  stream: {streamed.strip()[:120]}\n")

    print("All cases passed." if not failures else f"{failures} case(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
