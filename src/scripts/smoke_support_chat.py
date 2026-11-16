"""Public-FAQ smoke checks; --live calls OpenAI without reading account data."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from time import perf_counter

from sqlalchemy.ext.asyncio import async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backend.clients.ai_backend_client import AIBackendClient  # noqa: E402
from src.backend.services.support_knowledge_service import SupportKnowledgeService  # noqa: E402
from src.backend_ai.services.support_openai_client import SupportOpenAIClient  # noqa: E402
from src.backend_ai.services.support_understanding_service import SupportUnderstandingService  # noqa: E402
from src.shared.config import get_settings  # noqa: E402
from src.shared.database.session import create_engine_from_settings  # noqa: E402
from src.shared.dtos.ai_internal import (  # noqa: E402
    SupportClarificationDecision,
    SupportFAQAgentOutput,
    SupportFAQReview,
    SupportUnderstandingInput,
    SupportUnderstandingOutput,
)
from src.shared.dtos.support_chat import SupportChatMessage  # noqa: E402
from src.shared.services.credential_crypto import ensure_credential_crypto  # noqa: E402
from src.shared.services.provider_credential_resolver import ProviderCredentialResolver  # noqa: E402
from src.shared.services.support_safety import redact_support_secrets  # noqa: E402


class PublicFAQSmokeClient(SupportOpenAIClient):
    """Trace only generated public FAQ, never request headers or provider bodies."""

    async def structured(self, **kwargs):
        result = await super().structured(**kwargs)
        if kwargs["schema"] is SupportFAQAgentOutput:
            print(
                json.dumps(
                    {
                        "public_faq_candidate": redact_support_secrets(result.message),
                        "supported": result.supported,
                        "source_ids": result.source_ids,
                    }
                )
            )
        if kwargs["schema"] is SupportFAQReview:
            print(json.dumps({"public_answer_review": result.model_dump()}))
        if kwargs["schema"] is SupportClarificationDecision:
            print(json.dumps({"public_clarification": result.model_dump()}))
        return result


async def main(live: bool, case: str = "all", remote: bool = False, trace: bool = False) -> None:
    live = live or remote
    knowledge = SupportKnowledgeService()
    checks = [
        ("Billing là gì?", "usage-and-billing"),
        ("Tạo khóa truy cập cho ứng dụng", "application-api-key"),
        ("Playground test routing", "playground"),
    ]
    if case not in {"all", "faq"}:
        checks = []
    settings = get_settings().model_copy(update={"support_chat_llm_enabled": live})
    client = None
    if live and not remote:
        engine = create_engine_from_settings(settings=settings)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                resolver = ProviderCredentialResolver(session, ensure_credential_crypto(settings))
                credential = await resolver.resolve_service("internal_openai")
                client = (PublicFAQSmokeClient if trace else SupportOpenAIClient)(
                    api_key=credential.api_key,
                    model=settings.support_chat_model,
                    timeout=settings.support_chat_llm_timeout_seconds,
                    base_url=credential.base_url,
                )
        finally:
            await engine.dispose()
    understanding = SupportUnderstandingService(settings, client=client)

    async def understand(payload: SupportUnderstandingInput) -> SupportUnderstandingOutput:
        if remote:
            body = await AIBackendClient(settings).post(
                "/internal/v1/support/understand",
                payload.model_dump(mode="json"),
            )
            return SupportUnderstandingOutput.model_validate(body)
        return await understanding.understand(payload)

    for question, expected in checks:
        result = await understand(
            SupportUnderstandingInput(
                question=question,
                documents=knowledge.documents(question),
            )
        )
        if live:
            if result.plan is None or result.plan.intent != "faq" or result.faq_answer is None:
                raise RuntimeError("Live document-grounded FAQ unavailable")
            if expected not in result.faq_answer.source_ids:
                raise RuntimeError("Live FAQ evidence mismatch")
            print(
                json.dumps(
                    {
                        "question": question,
                        "model": settings.support_chat_model,
                        "sources": result.faq_answer.source_ids,
                        "answer": result.faq_answer.message,
                        "grounded": True,
                    },
                    ensure_ascii=True,
                )
            )
            continue
        article = knowledge.retrieve(
            question,
            result.semantic_scores,
            semantic_threshold=settings.support_chat_semantic_threshold,
            reranked_id=result.faq_id,
        )
        actual = article.id if article else None
        print(
            json.dumps(
                {
                    "faq": actual,
                    "expected": expected,
                    "intent": result.plan.intent if result.plan else "fallback",
                    "dense": bool(result.semantic_scores),
                },
                ensure_ascii=True,
            )
        )
        if actual != expected:
            raise RuntimeError("FAQ smoke mismatch")
    if live and case in {"all", "context"}:
        continuation = await understand(
            SupportUnderstandingInput(
                question="Còn hôm qua?",
                previous_intent="usage",
                previous_period="today",
                previous_application_name="Alpha",
                documents=knowledge.documents("Còn hôm qua?"),
            )
        )
        if (
            continuation.plan is None
            or continuation.plan.intent != "usage"
            or continuation.plan.period != "yesterday"
            or not continuation.plan.follow_up
        ):
            raise RuntimeError("Structured continuation smoke mismatch")
        print(json.dumps({"follow_up": continuation.plan.model_dump()}))
        selection = await understand(
            SupportUnderstandingInput(
                question="test 23 nhưng mà tôi muốn xem ngày hôm qua thôi",
                previous_intent="application_statistics",
                previous_period="today",
                pending_slot="application",
                history=[
                    SupportChatMessage(
                        id="public-example",
                        role="assistant",
                        content="Bạn muốn xem thống kê application nào?",
                        created_at="2026-10-11T00:00:00Z",
                        status="needs_clarification",
                    )
                ],
                documents=knowledge.documents("test 23 hôm qua"),
            )
        )
        if (
            selection.plan is None
            or selection.plan.intent != "application_statistics"
            or selection.plan.period != "yesterday"
            or not selection.plan.follow_up
        ):
            raise RuntimeError("Application selection continuation smoke mismatch")
        print(json.dumps({"application_selection_context": True}))

    if live and case in {"all", "semantic", "reported"}:
        logging.getLogger("src.backend_ai.services.support_understanding_service").setLevel(logging.DEBUG)
        question = (
            "Tôi muốn khoanh một nhóm mô hình riêng cho ứng dụng của mình. Nó khác gì với danh mục chung? "
            "Nếu nhóm đó gặp lỗi, hệ thống có được tự chạy sang mô hình bên ngoài không?"
            if case == "reported"
            else "Khoanh mô hình cho riêng ứng dụng khác danh mục chung thế nào, lúc hỏng có chạy ra bên ngoài được không?"
        )
        started = perf_counter()
        semantic = await understand(
            SupportUnderstandingInput(
                question=question,
                documents=knowledge.documents(question),
            )
        )
        print(
            json.dumps(
                {
                    "semantic_intent": semantic.plan.intent if semantic.plan else None,
                    "semantic_sources": semantic.faq_answer.source_ids if semantic.faq_answer else [],
                    "semantic_confidence": semantic.plan.confidence if semantic.plan else None,
                    "semantic_query": getattr(semantic.plan, "retrieval_query", None),
                    "elapsed_seconds": round(perf_counter() - started, 2),
                }
            )
        )
        if semantic.faq_answer is None or "model-pools" not in semantic.faq_answer.source_ids:
            raise RuntimeError("Semantic multi-document FAQ smoke mismatch")
        if not {"fallback-and-health", "constraints-and-policy"}.intersection(semantic.faq_answer.source_ids):
            raise RuntimeError("Semantic fallback evidence missing")
        print(json.dumps({"semantic_sources": semantic.faq_answer.source_ids, "answer": semantic.faq_answer.message}))

    if live and case == "knowledge":
        questions = [
            "Tôi dùng Python SDK để gọi Taskway. Hãy giải thích base_url, api_key, model auto và cách đọc câu trả lời; viết ví dụ ngắn, không chỉ chỉ tôi sang Applications.",
            "My embeddings have 1536 dimensions but my vector database expects 3072. How should I configure Taskway, and what do I need to check before migrating existing vectors?",
        ]
        for question in questions:
            started = perf_counter()
            result = await understand(SupportUnderstandingInput(question=question, documents=knowledge.documents(question)))
            if result.faq_answer is None:
                raise RuntimeError("Public documentation reasoning unavailable")
            sources = result.faq_answer.source_ids
            articles = {article.id: article for article in knowledge.articles()}
            if not any(articles[source_id].kind == "documentation" for source_id in sources):
                raise RuntimeError("Public documentation evidence missing")
            print(json.dumps({
                "question": question, "model": settings.support_chat_model,
                "sources": sources, "answer": result.faq_answer.message,
                "elapsed_seconds": round(perf_counter() - started, 2), "grounded": True,
            }, ensure_ascii=True))

    if live and case == "guided":
        first_question = "Nó không chạy, giúp tôi sửa với"
        first = await understand(SupportUnderstandingInput(question=first_question, documents=knowledge.documents(first_question)))
        if first.clarification is None or first.plan.intent != "clarify":
            raise RuntimeError("Reasoned follow-up missing for vague symptoms")
        print(json.dumps({"follow_up_question": first.clarification.model_dump()}, ensure_ascii=True))
        reply = "Mình muốn gọi Chat Completions bằng Python SDK, nhưng chưa tạo application hay API key. Hướng dẫn mình bắt đầu và cho ví dụ code."
        result = await understand(SupportUnderstandingInput(
            question=reply, documents=knowledge.documents(reply), history=[
                SupportChatMessage(id="vague-user", role="user", content=first_question, created_at="2026-10-14T00:00:00Z"),
                SupportChatMessage(id="follow-up", role="assistant", content=first.clarification.message, status="needs_clarification", created_at="2026-10-14T00:00:01Z"),
            ],
        ))
        if result.faq_answer is None or "create_application" not in result.faq_answer.navigation_ids:
            raise RuntimeError("Contextual answer or suggested application route missing")
        print(json.dumps({"guided_answer": result.faq_answer.model_dump()}, ensure_ascii=True))

    if live and case in {"all", "vague"}:
        vague = "Giúp tôi với"
        clarification = await understand(
            SupportUnderstandingInput(
                question=vague,
                documents=knowledge.documents(vague),
            )
        )
        if clarification.plan is None or clarification.plan.intent != "clarify":
            raise RuntimeError("Vague question scope smoke mismatch")
        print(json.dumps({"vague_question_requires_scope_choices": True}))
    print("Support smoke passed (live=" + str(live) + ")")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Call OpenAI using the internal_openai DB credential")
    mode.add_argument("--remote", action="store_true", help="Test the running AI backend using public FAQ only")
    parser.add_argument(
        "--trace", action="store_true", help="Print generated public FAQ candidates in direct live mode"
    )
    parser.add_argument(
        "--repeat", type=int, choices=range(1, 6), default=1, help="Repeat public smoke checks up to five times"
    )
    parser.add_argument("--case", choices=("all", "faq", "context", "semantic", "reported", "vague", "knowledge", "guided"), default="all")
    arguments = parser.parse_args()
    try:
        logging.basicConfig(level=logging.WARNING)
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        for iteration in range(arguments.repeat):
            print(json.dumps({"smoke_iteration": iteration + 1}))
            asyncio.run(main(arguments.live, arguments.case, arguments.remote, arguments.trace))
    except Exception as error:
        print("Support smoke failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
