"""Defense in depth for support input, evidence and generated text.

Pattern matching is a fast rejection layer, not an authorization boundary.
Models never receive database credentials, permissions or executable tools.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

from src.shared.dtos.support_chat import SupportChatMessage, SupportChatResponse

_SECRET = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|(?:ak|off)_[A-Za-z0-9_-]{24,}|AIza[A-Za-z0-9_-]{20,}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"(?:Bearer\s+)[A-Za-z0-9._-]{12,})",
    re.IGNORECASE,
)
_ASSIGNED_SECRET = re.compile(
    r"\b((?:OPENAI_API_KEY|GEMINI_API_KEY|AI_SERVICE_TOKEN|api[_ -]?key|secret|password)\s*[:=]\s*)"
    r"([^\s,;]+)",
    re.IGNORECASE,
)
_ATTACK = re.compile(
    r"(?:ignore|disregard|forget|override|bypass).{0,70}(?:instruction|rule|system|permission|safety)|"
    r"(?:bo qua|quen|vo hieu|ghi de).{0,70}(?:huong dan|quy tac|chi thi|quyen|bao mat)|"
    r"(?:system prompt|developer message|hidden instruction|prompt noi bo|huong dan an)|"
    r"(?:reveal|print|show|dump|expose|hien thi|tiet lo|in ra).{0,60}(?:secret|password|api.?key|token dich vu)|"
    r"(?:act as|you are now|dong vai|tu gio ban la).{0,35}(?:admin|system|developer|root)|"
    r"(?:other users?|another account|all users|tai khoan khac|nguoi dung khac).{0,50}(?:data|billing|usage|du lieu)|"
    r"(?:data|billing|usage|du lieu).{0,50}(?:other users?|another account|all users|tai khoan khac|nguoi dung khac)|"
    r"(?:<\|(?:im_start|system)|\[/?INST\]|<\s*/?\s*(?:system|developer)\s*>)",
    re.IGNORECASE | re.DOTALL,
)


def normalize_safety_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Cf")
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in value if not unicodedata.combining(c)).replace("đ", "d")


def is_prompt_attack(value: str) -> bool:
    return bool(_ATTACK.search(normalize_safety_text(value)))


def redact_support_secrets(value: str) -> str:
    def assignment(match: re.Match) -> str:
        assigned = match.group(2)
        # Public SDK examples use environment references and literal example
        # placeholders. These are not credentials; real values stay redacted.
        if re.fullmatch(r'''(?:os\.environ\[["'][A-Z][A-Z0-9_]*["']\]|process\.env\.[A-Z][A-Z0-9_]*)''', assigned):
            return match.group(0)
        if assigned.strip("\"'") in {"YOUR_APPLICATION_KEY", "YOUR_API_KEY", "your_application_key", "api_key"}:
            return match.group(0)
        return match.group(1) + "[REDACTED]"

    return _ASSIGNED_SECRET.sub(assignment, _SECRET.sub("[REDACTED]", value))


def safe_support_history(
    history: Sequence[SupportChatMessage],
    *,
    max_messages: int = 24,
    max_chars: int = 24000,
) -> list[SupportChatMessage]:
    """Exclude historic private facts and malicious turns from model context."""
    result: list[SupportChatMessage] = []
    remaining = max_chars
    skip_refused_user = False
    for message in reversed(history[-max_messages:]):
        if message.role == "assistant" and message.status == "insufficient_evidence":
            skip_refused_user = True
            continue
        if message.role == "user" and skip_refused_user:
            skip_refused_user = False
            continue
        reviewed_faq = (
            message.role == "assistant"
            and message.status == "answered"
            and any(source.kind in {"faq", "documentation"} for source in message.sources)
        )
        if not reviewed_faq and is_prompt_attack(message.content):
            continue
        if message.role == "assistant" and any(s.kind == "database" for s in message.sources):
            content = "[Đã trả lời dữ liệu tài khoản; phải truy vấn lại theo quyền hiện tại.]"
        else:
            content = redact_support_secrets(message.content)[:1500]
        if len(content) > remaining:
            break
        remaining -= len(content)
        result.append(
            message.model_copy(update={"content": content, "sources": [], "actions": [], "choices": [], "data": None})
        )
    return list(reversed(result))


def validate_support_rewrite(text: str, response: SupportChatResponse, source_ids: list[str]) -> bool:
    """Reject changed numbers, invented sources/links, credentials and instructions."""
    if not text.strip() or len(text) > 6000 or is_prompt_attack(text):
        return False
    if redact_support_secrets(text) != text:
        return False
    expected_ids = {source.id for source in response.sources}
    if set(source_ids) != expected_ids:
        return False

    def numbers(value: str) -> Counter:
        return Counter(re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?!\w)", value))

    if numbers(text) != numbers(response.message):
        return False
    if re.search(r"https?://|www\.|javascript:|data:|<[^>]+>|\]\(", text, re.IGNORECASE):
        return False
    return True


def validate_support_faq_answer(text: str, source_ids: list[str], evidence: dict[str, str]) -> bool:
    """Generated FAQ may select facts from multiple documents, not invent sources or secrets."""
    return support_faq_rejection_reason(text, source_ids, evidence) is None


def validate_support_clarification(message: str, choices: Sequence) -> bool:
    """Questions contain no URLs, secrets or requests to submit credentials."""
    if not message.strip() or len(message) > 1200 or len(choices) > 4:
        return False
    for text in [message, *(choice.label for choice in choices), *(choice.message for choice in choices)]:
        if is_prompt_attack(text) or redact_support_secrets(text) != text:
            return False
        if re.search(r"https?://|www\.|javascript:|data:|<[^>]+>|\]\(", text, re.IGNORECASE):
            return False
        normalized = normalize_safety_text(text)
        # Match requests for credential values, not safe requests such as
        # "send an error message with API keys redacted".
        if re.search(r"(?:send|share|paste|provide|enter|gui|dan|nhap|cung cap)\s+(?:(?:me|your|the|actual|real|cho|toi|cua|ban|gia|tri|that)\s+){0,4}(?:api.?key|password|mat khau|access token|credential|secret)", normalized):
            return False
    return True


def support_faq_rejection_reason(text: str, source_ids: list[str], evidence: dict[str, str]) -> str | None:
    """Safe diagnostic categories only: never include generated content in logs."""
    if not text.strip() or len(text) > 6000:
        return "invalid_length"
    # Input-attack patterns are not output policies: reviewed documentation may
    # legitimately explain "never bypass permissions" or "no internal prompts".
    # FAQ facts and instructions must instead pass document entailment review.
    # Credentials, invented citations and executable/link markup remain rejected.
    if redact_support_secrets(text) != text:
        return "secret"
    if not source_ids or len(source_ids) != len(set(source_ids)) or any(key not in evidence for key in source_ids):
        return "invalid_sources"
    narrative = re.sub(r"```.*?```|`[^`]*`", "", text, flags=re.DOTALL)
    if re.search(r"www\.|javascript:|data:|<[^>]+>|\]\(", narrative, re.IGNORECASE):
        return "link_or_markup"
    url_pattern = r'''https?://[^\s`"'<>\])]+'''
    known_urls = set(re.findall(url_pattern, "\n".join(evidence[key] for key in source_ids)))
    if not set(re.findall(url_pattern, text)) <= known_urls:
        return "link_or_markup"
    return None
