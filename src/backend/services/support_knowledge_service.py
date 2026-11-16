from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from math import log
from pathlib import Path
from typing import Literal
from unicodedata import combining, normalize

from src.shared.config import get_settings
from src.shared.dtos.ai_internal import SupportRetrievalDocument
from src.shared.services.support_documentation import load_public_documentation
from src.shared.services.support_retrieval import fuse_support_ranks
from src.shared.services.taskway_documentation import DOCUMENTATION_CHAPTERS


@dataclass(frozen=True)
class SupportArticle:
    id: str
    title: str
    keywords: tuple[str, ...]
    answer: str
    url: str | None = None
    suggestions: tuple[str, ...] = ()
    documentation: tuple[str, ...] = ()
    kind: Literal["faq", "documentation"] = "faq"


def normalize_support_text(value: str) -> str:
    """Make Vietnamese and English keyword retrieval accent/case insensitive."""

    decomposed = normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not combining(character)).replace("đ", "d")


def is_assistant_model_question(question: str) -> bool:
    """Public assistant identity, distinct from an application/catalog model request."""
    text = normalize_support_text(question)
    if any(phrase in text for phrase in ("danh sach", "available model", "kha dung", "application", "ung dung")):
        return False
    topic = any(term in text for term in ("model", "mo hinh", "openai", "gemini", "gpt"))
    self_reference = any(
        phrase in text
        for phrase in (
            "ban dung",
            "ban dang dung",
            "ban su dung",
            "ban dang su dung",
            "ban dang la",
            "ban chay",
            "ban dang chay",
            "ban la",
            "cua ban",
            "chatbot",
            "tro ly",
            "you using",
            "you use",
            "your model",
            "powers you",
            "you running",
            "are you",
            "what are you",
        )
    )
    return topic and (
        self_reference or text.strip(" ?.!\n") in {"model gi", "model nao", "mo hinh gi", "what model", "which model"}
    )


def is_assistant_capability_question(question: str) -> bool:
    """Whether the user also asks what the support assistant does."""
    text = normalize_support_text(question)
    return any(
        phrase in text
        for phrase in (
            "co tac dung gi",
            "giup duoc gi",
            "lam duoc gi",
            "nhiem vu gi",
            "vai tro gi",
            "what can you do",
            "what do you do",
            "your purpose",
            "your role",
        )
    )


def prefers_vietnamese(question: str) -> bool:
    """Choose a safe fallback language; normal FAQ answers still use the LLM language policy."""
    if re.search(r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]", question.casefold()):
        return True
    tokens = set(re.findall(r"\w+", normalize_support_text(question)))
    vietnamese = {"ban", "toi", "minh", "la", "gi", "co", "tac", "dung", "the", "muon", "biet", "giup", "duoc"}
    english = {"you", "your", "what", "which", "model", "can", "do", "are", "purpose", "role", "how", "use", "install", "configure", "read"}
    return len(tokens & vietnamese) > len(tokens & english)


def _load_articles() -> tuple[SupportArticle, ...]:
    """Load reviewed product documentation shipped with both backend images."""
    path = Path(__file__).resolve().parents[2] / "shared" / "resources" / "support_faq.md"
    text = path.read_text(encoding="utf-8")
    articles = []
    for section in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        heading, body = section.split("\n", 1)
        article_id, title = heading.split(" | ", 1)
        metadata = dict(re.findall(r"<!-- (url|docs|keywords|suggestions): (.*?) -->", body))
        answer = re.sub(r"<!--.*?-->\s*", "", body, flags=re.DOTALL).strip()
        url = metadata.get("url") or None
        if url and (not url.startswith("/") or url.startswith("//")):
            raise ValueError("FAQ documentation requires a local product URL")
        documentation = tuple(filter(None, metadata.get("docs", "").split(" | ")))
        if any(chapter not in DOCUMENTATION_CHAPTERS for chapter in documentation):
            raise ValueError("FAQ documentation requires reviewed Taskway chapters")
        articles.append(
            SupportArticle(
                id=article_id.strip(),
                title=title.strip(),
                keywords=tuple(filter(None, metadata.get("keywords", "").split(" | "))),
                answer=answer,
                url=url,
                suggestions=tuple(filter(None, metadata.get("suggestions", "").split(" | "))),
                documentation=documentation,
            )
        )
    if not articles or len({a.id for a in articles}) != len(articles):
        raise ValueError("FAQ documentation requires unique chapter IDs")
    return tuple(articles)


SUPPORT_ARTICLES = _load_articles() + tuple(
    SupportArticle(
        id=chunk.id, title=chunk.title, keywords=(), answer=chunk.text, url=chunk.url, kind="documentation",
    )
    for chunk in load_public_documentation()
)

class SupportKnowledgeService:
    """BM25 sparse retrieval + dense cosine scores fused with weighted RRF."""

    @staticmethod
    def articles() -> tuple[SupportArticle, ...]:
        return SUPPORT_ARTICLES

    _STOP_WORDS = frozenset(
        "toi ban cua la gi va cho ve mot cac nhung the nao de co the dung nay minh con hom qua how what is the a an of to my".split()
    )
    _ALIASES = {
        "khoa truy cap": "api key",
        "khoa api": "api key",
        "thu nghiem": "playground",
        "muc su dung": "usage",
        "ung dung": "application",
    }

    @staticmethod
    def answer_for(article: SupportArticle) -> str:
        if article.id != "assistant-model":
            return article.answer
        settings = get_settings()
        message = (
            f"Gateway Assistant is configured to use OpenAI's {settings.support_chat_model} model. "
            "The model understands conversational context and answers from the FAQ knowledge base; account data is queried and permission-checked by the backend."
        )
        if not settings.support_chat_llm_enabled:
            message += " The AI layer is currently disabled, so answers are handled by the FAQ and backend data services."
        return message

    @staticmethod
    def assistant_identity_answer(question: str, *, include_capabilities: bool) -> str:
        settings = get_settings()
        if prefers_vietnamese(question):
            message = (
                f"Tôi là Gateway Assistant, hiện được cấu hình sử dụng model OpenAI {settings.support_chat_model}. "
                "Tôi hiểu ngữ cảnh hội thoại và trả lời dựa trên kho kiến thức FAQ của Gateway."
            )
            if include_capabilities:
                message += (
                    " Tôi có thể giải thích hành vi hệ thống, hướng dẫn thao tác và đọc dữ liệu application, "
                    "usage hoặc billing mà tài khoản của bạn được phép xem."
                )
            if not settings.support_chat_llm_enabled:
                message += " Hiện lớp AI đang tắt nên câu trả lời được xử lý bằng FAQ và các dịch vụ dữ liệu backend."
            return message
        message = (
            f"I am Gateway Assistant, currently configured to use OpenAI's {settings.support_chat_model} model. "
            "I understand conversation context and answer from the Gateway FAQ knowledge base."
        )
        if include_capabilities:
            message += (
                " I can explain documented system behavior, guide you through product tasks, and read application, "
                "usage, or billing data that your account is allowed to view."
            )
        if not settings.support_chat_llm_enabled:
            message += " The AI layer is currently disabled, so answers are handled by the FAQ and backend data services."
        return message

    @classmethod
    def _tokens(cls, text: str) -> list[str]:
        text = normalize_support_text(text)
        for phrase, replacement in cls._ALIASES.items():
            text = text.replace(phrase, replacement)
        return [token for token in re.findall(r"\w+", text) if token not in cls._STOP_WORDS]

    @staticmethod
    def _document(article: SupportArticle) -> str:
        return f"{article.title}\n{article.title}\n{' '.join(article.keywords)}\n{article.answer}"

    def lexical_scores(self, question: str, *, articles: tuple[SupportArticle, ...] | None = None) -> dict[str, float]:
        articles = articles if articles is not None else SUPPORT_ARTICLES
        documents = [Counter(self._tokens(self._document(article))) for article in articles]
        query = set(self._tokens(question))
        # A greeting before a product question should not outrank the actual
        # topic as the FAQ corpus grows. Keep greeting-only queries intact.
        topic_tokens = query - {"hello", "hi", "hey", "bro", "xin", "chao"}
        query = topic_tokens or query
        average_length = sum(sum(doc.values()) for doc in documents) / len(documents)
        scores = {}
        for article, doc in zip(articles, documents, strict=True):
            length = sum(doc.values())
            score = 0.0
            for term in query:
                frequency = doc[term]
                if not frequency:
                    continue
                document_frequency = sum(term in other for other in documents)
                inverse_frequency = log(1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5))
                score += (
                    inverse_frequency * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / average_length))
                )
            scores[article.id] = score
        return scores

    def documents(self, question: str) -> list[SupportRetrievalDocument]:
        scores = self.lexical_scores(question)
        return [
            SupportRetrievalDocument(id=a.id, text=f"{a.title}\n{self.answer_for(a)}", lexical_score=scores[a.id])
            for a in SUPPORT_ARTICLES
        ]

    def retrieve(
        self,
        question: str,
        semantic_scores: dict[str, float] | None = None,
        *,
        semantic_threshold: float = 0.35,
        reranked_id: str | None = None,
    ) -> SupportArticle | None:
        # This method serves the legacy degraded path when no model is online.
        # The reasoning agent receives all FAQ + public documentation chunks.
        candidates = SUPPORT_ARTICLES if semantic_scores else tuple(a for a in SUPPORT_ARTICLES if a.kind == "faq")
        lexical = self.lexical_scores(question, articles=candidates)
        dense = semantic_scores or {}
        ranking = fuse_support_ranks(lexical, dense, threshold=semantic_threshold)
        if not ranking:
            return None
        winner = reranked_id if reranked_id in ranking[:3] else ranking[0]
        article = next(article for article in SUPPORT_ARTICLES if article.id == winner)
        return replace(article, answer=self.answer_for(article))
