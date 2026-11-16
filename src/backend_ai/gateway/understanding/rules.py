from __future__ import annotations

import re

from src.backend_ai.gateway.understanding.base import BaseUnderstander, UnderstandingOutcome
from src.shared.dtos.gateway import Difficulty, EffectiveConstraints, GatewayRequest, SemanticAnalysis, TaskType

_TASK_PATTERNS: dict[TaskType, tuple[str, ...]] = {
    TaskType.SUMMARIZATION: (r"\b(summar(?:y|ize)|tóm tắt)\b",),
    TaskType.CONTENT_GENERATION: (r"\b(write|draft|create|viết|soạn|tạo nội dung)\b",),
    TaskType.DATA_EXTRACTION: (r"\b(extract|parse|fields?|trích xuất|bóc tách)\b",),
    TaskType.REASONING: (r"\b(analy[sz]e|reason|compare|evaluate|phân tích|so sánh|đánh giá)\b",),
    TaskType.CODING: (
        r"\b(code|implement|debug|function|python|javascript|lập trình|sửa lỗi)\b",
        r"\b(build|create|design|write)\s+(?:an?\s+)?(?:api|endpoint|function|script|service)\b",
    ),
    TaskType.TRANSLATION: (r"\b(translate|translation|dịch)\b",),
    TaskType.CLASSIFICATION: (r"\b(classif(?:y|ication)|categorize|phân loại)\b",),
    TaskType.RESEARCH: (r"\b(research|find sources?|literature|nghiên cứu|tìm nguồn)\b",),
    TaskType.CUSTOMER_SUPPORT: (r"\b(customer|ticket|refund|support|khách hàng|hoàn tiền|khiếu nại)\b",),
}
_INDEPENDENT_CONNECTORS = re.compile(
    r"(?:\balso\b|\badditionally\b|\bin parallel\b|\bđồng thời\b|\bngoài ra\b|\bvà cũng\b|[;\n]\s*[-*]?\s*)",
    re.I,
)
_SEQUENTIAL_CONNECTORS = re.compile(r"(?:\bthen\b|\bafter that\b|\bfirst\b|\bnext\b|\bsau đó\b|\btrước tiên\b)", re.I)
_DELIVERABLE_MARKERS = re.compile(
    r"(?:\breport\b|\bsummary\b|\btable\b|\bcode\b|\bemail\b|báo cáo|bảng|mã nguồn)", re.I
)


def detect_task_types(prompt: str) -> list[TaskType]:
    task_types = [
        task_type
        for task_type, patterns in _TASK_PATTERNS.items()
        if any(re.search(pattern, prompt, re.I) for pattern in patterns)
    ]
    return task_types or [TaskType.GENERAL]


def required_capabilities_for_prompt(
    prompt: str,
    *,
    has_context: bool = False,
    output_format: str = "text",
) -> list[str]:
    """Derive model capabilities for one prompt or one decomposed goal.

    Capabilities are intentionally calculated at task scope. A multi-task request
    may contain one coding task alongside ordinary text tasks, and those tasks
    should not all inherit the coding requirement.
    """
    task_types = detect_task_types(prompt)
    capabilities: list[str] = []
    if TaskType.CODING in task_types:
        capabilities.append("code_generation")
    if TaskType.REASONING in task_types:
        capabilities.append("reasoning")
    # Research does not require "web_search": no executor has a search tool,
    # and DatabaseRoutingService hard-filters on capabilities, so the
    # requirement only made every "research"/"nghiên cứu" prompt unroutable.
    # Add it back once a model with live search is wired to an executor.
    if has_context:
        capabilities.append("long_context")
    if output_format != "text":
        capabilities.append("structured_output")
    return capabilities


def estimate_task_difficulty(prompt: str, fallback: Difficulty = Difficulty.EASY) -> Difficulty:
    """Estimate one goal independently so mixed requests can use mixed model tiers."""
    normalized = prompt.lower()
    word_count = len(prompt.split())
    requested_length = max(
        (int(value) for value in re.findall(r"\b(\d{3,6})\s*(?:words?|từ)\b", normalized)),
        default=0,
    )
    hard_markers = re.search(
        r"\b(architecture|formal proof|root cause|multi-stage|chứng minh|kiến trúc|"
        r"báo cáo tài chính|financial report|rủi ro kinh doanh)\b",
        normalized,
        re.I,
    )
    if word_count > 800 or requested_length >= 1000 or hard_markers:
        return Difficulty.HARD

    task_types = detect_task_types(prompt)
    if word_count > 180 or any(
        task_type in {TaskType.CODING, TaskType.REASONING, TaskType.RESEARCH}
        for task_type in task_types
    ):
        return Difficulty.MEDIUM
    return Difficulty.EASY if fallback != Difficulty.HARD else Difficulty.MEDIUM


class RuleUnderstander(BaseUnderstander):
    name = "rule"

    async def understand(self, request: GatewayRequest, constraints: EffectiveConstraints) -> UnderstandingOutcome:
        prompt = request.focus_text()
        task_types = detect_task_types(prompt)

        independent_count = len(_INDEPENDENT_CONNECTORS.findall(prompt))
        sequential_count = len(_SEQUENTIAL_CONNECTORS.findall(prompt))
        numbered_items = len(re.findall(r"(?m)^\s*(?:\d+[.)]|[-*])\s+", prompt))
        deliverables = len(set(match.lower() for match in _DELIVERABLE_MARKERS.findall(prompt)))
        goal_count = max(1, min(20, 1 + independent_count + min(numbered_items, 4)))

        reasoning_depth = 0
        if re.search(r"\b(explain|analy[sz]e|compare|why|phân tích|giải thích|so sánh)\b", prompt, re.I):
            reasoning_depth = 1
        if re.search(r"\b(trade-?offs?|step.by.step|prove|architecture|root cause|đánh đổi|kiến trúc)\b", prompt, re.I):
            reasoning_depth = 2
        if len(prompt) > 2500 or re.search(r"\b(formal proof|optimization|multi-stage|chứng minh)\b", prompt, re.I):
            reasoning_depth = 3

        domain_specialization = 0
        if re.search(
            r"\b(api|database|legal|medical|financial|sql|clinical|kubernetes|pháp lý|y khoa)\b", prompt, re.I
        ):
            domain_specialization = 2
        if re.search(r"\b(hipaa|gdpr|pci-dss|basel|fda|iso 27001|luật|nghị định)\b", prompt, re.I):
            domain_specialization = 3

        # Coarse stand-in for jev's risk score: regulated domains and
        # irreversible operations are where a wrong answer costs most.
        risk_level = 0
        if re.search(r"\b(production|prod|migrat\w*|delete|drop table|payment|thanh toán|xoá dữ liệu)\b", prompt, re.I):
            risk_level = 1
        if domain_specialization == 3 or re.search(
            r"\b(legal|medical|clinical|diagnos\w*|lawsuit|contract|pháp lý|y khoa|hợp đồng|chẩn đoán)\b", prompt, re.I
        ):
            risk_level = 2

        output_complexity = min(3, max(0, deliverables - 1) + int(constraints.constraints.output_format != "text"))
        if re.search(r"\b(schema|citations?|chart|dashboard|structured|biểu đồ|trích dẫn)\b", prompt, re.I):
            output_complexity = min(3, output_complexity + 1)

        vague = len(re.findall(r"\b(it|this|that|something|good|better|nó|cái này|tốt hơn)\b", prompt, re.I))
        ambiguity = min(3, vague + int(len(prompt.split()) < 4))
        if re.search(r"\b(clarify|if unclear|ask me|làm rõ)\b", prompt, re.I):
            ambiguity = max(ambiguity, 2)

        independence = min(3, independent_count + int(numbered_items > 1))
        if sequential_count and not independent_count:
            independence = max(0, independence - 1)
        dependency_clarity = min(3, sequential_count + int(numbered_items > 1) + int("depends on" in prompt.lower()))
        if goal_count == 1:
            dependency_clarity = 3

        capabilities = required_capabilities_for_prompt(
            prompt,
            has_context=bool(request.context),
            output_format=constraints.constraints.output_format,
        )

        analysis = SemanticAnalysis(
            goal_count=goal_count,
            task_types=task_types,
            reasoning_depth=reasoning_depth,
            task_independence=independence,
            dependency_clarity=dependency_clarity,
            domain_specialization=domain_specialization,
            output_complexity=output_complexity,
            ambiguity_level=ambiguity,
            required_capabilities=capabilities,
            risk_level=risk_level,
            explicit_constraints=constraints.constraints.model_dump(mode="json", exclude_none=True),
            explanations=[
                f"Detected {goal_count} goal(s) and {len(task_types)} task type(s) with deterministic rules.",
                f"Independent connectors={independent_count}, sequential connectors={sequential_count}.",
            ],
        )
        return UnderstandingOutcome(analysis=analysis, strategy=self.name)
