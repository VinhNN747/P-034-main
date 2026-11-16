from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.backend_ai.gateway.core.config import GatewayConfig
from src.shared.dtos.gateway import (
    EvidenceSpan,
    ExplicitGoal,
    GoalExtractionResult,
    PromptSegment,
    PromptSegmentKind,
)

_LIST_MARKER_RE = re.compile(r"(?m)(?:^|\n)[ \t]*(?P<marker>[-*•]|\d+[.)])[ \t]+")
_INLINE_NUMBER_RE = re.compile(r"(?<!\w)(?P<marker>\d+[.)])[ \t]+")
_DELIMITER_RE = re.compile(r"[;\n]+|(?<=[.!?])\s+(?=[A-ZÀ-Ỵ])")
_CONNECTOR_RE = re.compile(
    r"\b(?:and|also|additionally|then|next|và|và cũng|ngoài ra|đồng thời|sau đó|tiếp theo)\b",
    re.I,
)
_SEQUENTIAL_RE = re.compile(
    r"\b(?:then|next|after(?:wards| that)?|using (?:the )?result|based on (?:the )?result|"
    r"sau đó|tiếp theo|dựa trên kết quả|sử dụng kết quả|từ kết quả)\b",
    re.I,
)
_GOAL_RE = re.compile(
    r"\b(?:summar(?:ize|ise)|write|draft|create|generate|calculate|compute|analy[sz]e|compare|evaluate|"
    r"extract|parse|translate|classif(?:y|ication)|research|find|identify|explain|diagnose|recommend|list|outline|"
    r"implement|code|debug|design|build|produce|describe|point out|suggest|rewrite|propose|convert|solve|"
    r"tóm tắt|viết|soạn|(?<!nhân )(?<!sáng )tạo|(?<!mãn )(?<!cấp )tính(?!\s+(?:năng|chất|cách))|phân tích|so sánh|đánh giá|"
    r"trích xuất|dịch|phân loại|nghiên cứu|tìm|xác định|giải thích|chẩn đoán|đề xuất|liệt kê|phác thảo|"
    r"triển khai|lập trình|sửa lỗi|thiết kế|xây dựng|chỉ ra|nêu|trình bày|đưa ra|rút ra|kể tên|mô tả|gợi ý|"
    r"chuyển đổi|sắp xếp|cho biết|trả lời|viết lại|tổng hợp)\b",
    re.I,
)
# Header that introduces the material the tasks operate on ("Đoạn văn:", "Text:").
# Everything it introduces is shared input, not goal text, so it is split off
# before goal segmentation and handed to every subtask at execution.
_SOURCE_HEADER_RE = re.compile(
    r"(?im)^[ \t]*(?:đoạn văn|văn bản|nội dung|bài viết|bài báo|dữ liệu|ngữ liệu|tài liệu|bài thơ|câu chuyện|"
    r"passage|text|article|input|data|content|document|context)"
    r"(?:[ \t]+(?:sau|sau đây|dưới đây|bên dưới|cần xử lý|below|to (?:analy[sz]e|summari[sz]e|use)))?"
    r"[ \t]*[:：]"
)
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")
# A numbered/bulleted item is a deliverable unless it is a label ("Đối tượng: học sinh").
_LABEL_ITEM_RE = re.compile(r"^[^:\n]{1,30}:\s")
_NUMBER_WORDS = {
    "hai": 2, "ba": 3, "bốn": 4, "năm": 5, "sáu": 6, "bảy": 7, "tám": 8, "chín": 9, "mười": 10,
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
_DECLARED_COUNT_RE = re.compile(
    r"\b(?P<count>\d+|" + "|".join(_NUMBER_WORDS) + r")\s+"
    r"(?:nhiệm vụ|yêu cầu|câu hỏi|tác vụ|việc|tasks?|questions?|requests?|things|deliverables?)\b",
    re.I,
)
# Trailing text after the last list item, separated by a blank line and at
# least this long, is treated as unlabeled source material.
_MIN_TRAILING_SOURCE_CHARS = 80
_PROCESS_RE = re.compile(
    r"\b(?:read|load|open|collect|gather|inspect|prepare|clean|normalize|fetch|"
    r"đọc|tải|mở|thu thập|kiểm tra|chuẩn bị|làm sạch|chuẩn hóa|lấy dữ liệu)\b",
    re.I,
)
_OUTPUT_CONSTRAINT_RE = re.compile(
    r"(?:\b(?:output|return|respond|answer|format|schema|trả về|phản hồi|định dạng)\b.{0,35}"
    r"\b(?:json|csv|xml|html|markdown|text)\b|"
    r"\b(?:in|using|bằng|theo|dưới)\b.{0,20}\b(?:english|vietnamese|tiếng việt|json|csv|xml|html|markdown)\b|"
    r"\b(?:no more than|maximum|under|không quá|tối đa)\s+\d+\s+(?:words?|từ|tokens?)\b)",
    re.I,
)
_STYLE_RE = re.compile(
    r"\b(?:polite|formal|informal|concise|brief|friendly|professional|academic|"
    r"lịch sự|trang trọng|thân thiện|chuyên nghiệp|ngắn gọn|súc tích|học thuật|văn phong|giọng điệu)\b",
    re.I,
)
_VAGUE_ONLY_RE = re.compile(
    r"^(?:do|handle|process|fix|improve|làm|xử lý|sửa|cải thiện)\s+(?:it|this|that|nó|cái này)\W*$",
    re.I,
)


@dataclass(frozen=True)
class _RawSegment:
    start: int
    end: int
    source: str
    sequential: bool = False


_REQUEST_WORD_RE = re.compile(
    r"\b(?:hãy|xin|giúp|cho (?:tôi|em|mình|cháu) (?:biết|hỏi)|làm (?:sao|thế nào)|như thế nào|thế nào|"
    r"có nên|có phải|có thể|bao nhiêu|là gì|ở đâu|tại sao|vì sao|khi nào|please|how|what|why|which|when|where|"
    r"who|can you|could you)\b",
    re.I,
)


def looks_like_request(prompt: str, start: int, end: int) -> bool:
    """Whether prompt[start:end] asks for something: a question, an action verb,
    a request word, or an item of an explicit list. Background sentences ("Tôi bị
    viêm họng, đã đi khám nhưng không khỏi") are none of these."""
    text = prompt[start:end]
    line_start = prompt.rfind("\n", 0, start) + 1
    in_list = bool(_LIST_MARKER_RE.match(prompt, line_start)) or bool(
        re.search(r"(?:^|\s)(?:[-*•]|\d+[.)])\s*$", prompt[line_start:start])
    )
    question = "?" in text or prompt[end : end + 2].lstrip().startswith("?")
    return question or in_list or bool(_GOAL_RE.search(text)) or bool(_REQUEST_WORD_RE.search(text))


def has_compound_action(goal_text: str) -> bool:
    """A single extracted goal naming several actions ("phân tích doanh thu,
    so sánh với năm ngoái") may hide several deliverables the regex could not
    split; the LLM planner gets to look at it."""
    return len(_GOAL_RE.findall(goal_text)) >= 2


def normalize_prompt(prompt: str) -> str:
    """Normalize presentation without changing the original request kept on the result."""
    normalized = unicodedata.normalize("NFKC", prompt).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    return "\n".join(line for line in lines if line)


def _trimmed_span(prompt: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and prompt[start].isspace():
        start += 1
    while end > start and prompt[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _list_markers(prompt: str) -> list[re.Match[str]]:
    markers = list(_LIST_MARKER_RE.finditer(prompt))
    inline_numbers = list(_INLINE_NUMBER_RE.finditer(prompt))
    seen = {(match.start("marker"), match.end()) for match in markers}
    markers.extend(match for match in inline_numbers if (match.start("marker"), match.end()) not in seen)
    markers.sort(key=lambda match: match.start("marker"))
    return markers


def _marker_segments(prompt: str) -> list[_RawSegment]:
    markers = _list_markers(prompt)
    if not markers:
        return []

    segments: list[_RawSegment] = []
    prefix = _trimmed_span(prompt, 0, markers[0].start("marker"))
    if prefix:
        segments.append(_RawSegment(*prefix, source="list_prefix"))
    for index, marker in enumerate(markers):
        end = markers[index + 1].start("marker") if index + 1 < len(markers) else len(prompt)
        span = _trimmed_span(prompt, marker.end(), end)
        if span:
            text = prompt[span[0] : span[1]]
            segments.append(_RawSegment(*span, source="explicit", sequential=bool(_SEQUENTIAL_RE.search(text))))
    return segments


def _source_spans(prompt: str) -> list[tuple[int, int]]:
    """Spans holding the material the tasks operate on: fenced blocks, text
    under a header such as "Đoạn văn:", and an unlabeled passage pasted after
    the last list item. A passage glued to the last item used to become part
    of that goal and was never shown to the other subtasks."""
    fences = [(match.start(), match.end()) for match in _FENCE_RE.finditer(prompt)]

    def inside_fence(position: int) -> bool:
        return any(start <= position < end for start, end in fences)

    markers = [m for m in _LIST_MARKER_RE.finditer(prompt) if not inside_fence(m.start())]
    header_spans: list[tuple[int, int]] = []
    for header in _SOURCE_HEADER_RE.finditer(prompt):
        if inside_fence(header.start()) or any(start <= header.start() < end for start, end in header_spans):
            continue
        end = len(prompt)
        later_markers = [m for m in markers if m.start() >= header.end()]
        # Material placed before the task list stops at the blank line that
        # precedes the instructions introducing the list. Material after the
        # task list runs to the end, even if it has numbered lines of its own.
        if later_markers and not any(m.end() <= header.start() for m in markers):
            first_marker = later_markers[0].start("marker")
            blanks = list(_BLANK_LINE_RE.finditer(prompt, header.end(), first_marker))
            end = blanks[-1].start() if blanks else first_marker
        span = _trimmed_span(prompt, header.start(), end)
        if span:
            header_spans.append(span)

    spans = fences + header_spans
    if not header_spans:
        list_markers = _list_markers(_masked(prompt, fences))
        blank = _BLANK_LINE_RE.search(prompt, list_markers[-1].end()) if list_markers else None
        span = _trimmed_span(prompt, blank.end(), len(prompt)) if blank else None
        if span and span[1] - span[0] >= _MIN_TRAILING_SOURCE_CHARS and not inside_fence(span[0]):
            remainder = prompt[span[0] : span[1]]
            if not _GOAL_RE.match(remainder) and not _LIST_MARKER_RE.match(remainder):
                spans.append(span)
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _masked(prompt: str, spans: list[tuple[int, int]]) -> str:
    """Blank out spans while keeping offsets and line structure intact."""
    chars = list(prompt)
    for start, end in spans:
        for index in range(start, end):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _clip_to_sources(segment: _RawSegment, prompt: str, spans: list[tuple[int, int]]) -> _RawSegment | None:
    end = segment.end
    for start, _ in spans:
        if segment.start < start < end:
            end = start
    span = _trimmed_span(prompt, segment.start, end)
    if not span:
        return None
    return _RawSegment(span[0], span[1], source=segment.source, sequential=segment.sequential)


def _declared_goal_count(texts: list[str]) -> int | None:
    for text in texts:
        match = _DECLARED_COUNT_RE.search(text)
        if match:
            raw = match.group("count").casefold()
            count = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
            if count and count >= 2:
                return count
    return None


def _split_action_connectors(prompt: str, start: int, end: int, source: str) -> list[_RawSegment]:
    text = prompt[start:end]
    connectors = list(_CONNECTOR_RE.finditer(text))
    if not connectors or len(_GOAL_RE.findall(text)) < 2:
        span = _trimmed_span(prompt, start, end)
        return [_RawSegment(*span, source=source)] if span else []

    parts: list[_RawSegment] = []
    cursor = 0
    pending_sequential = False
    for connector in connectors:
        left = text[cursor : connector.start()]
        right = text[connector.end() :]
        if not _GOAL_RE.search(left) or not _GOAL_RE.search(right):
            continue
        span = _trimmed_span(prompt, start + cursor, start + connector.start())
        if span:
            parts.append(_RawSegment(*span, source="connector", sequential=pending_sequential))
        pending_sequential = bool(_SEQUENTIAL_RE.fullmatch(connector.group(0)))
        cursor = connector.end()
    span = _trimmed_span(prompt, start + cursor, end)
    if span:
        parts.append(_RawSegment(*span, source="connector", sequential=pending_sequential))
    return parts if len(parts) > 1 else [_RawSegment(start, end, source=source)]


def _raw_segments(prompt: str, sources: list[tuple[int, int]] | None = None) -> list[_RawSegment]:
    if sources:
        masked = _masked(prompt, sources)
        clipped = (_clip_to_sources(raw, prompt, sources) for raw in _raw_segments(masked))
        return [raw for raw in clipped if raw is not None]

    marked = _marker_segments(prompt)
    if marked:
        return marked

    chunks: list[_RawSegment] = []
    cursor = 0
    for delimiter in _DELIMITER_RE.finditer(prompt):
        span = _trimmed_span(prompt, cursor, delimiter.start())
        if span:
            chunks.extend(_split_action_connectors(prompt, *span, source="delimiter"))
        cursor = delimiter.end()
    span = _trimmed_span(prompt, cursor, len(prompt))
    if span:
        chunks.extend(_split_action_connectors(prompt, *span, source="prose"))
    return chunks


def _kind(text: str) -> PromptSegmentKind:
    has_goal = bool(_GOAL_RE.search(text))
    if has_goal and _STYLE_RE.search(text):
        remainder = _STYLE_RE.sub(" ", _GOAL_RE.sub(" ", text))
        remainder = re.sub(r"\b(?:a|an|the|with|in|using|với|bằng|theo)\b|\W+", " ", remainder, flags=re.I)
        if not remainder.strip():
            return PromptSegmentKind.STYLE_PREFERENCE
    if has_goal:
        return PromptSegmentKind.GOAL
    if _OUTPUT_CONSTRAINT_RE.search(text):
        return PromptSegmentKind.OUTPUT_CONSTRAINT
    if _STYLE_RE.search(text):
        return PromptSegmentKind.STYLE_PREFERENCE
    if _PROCESS_RE.search(text) or _SEQUENTIAL_RE.search(text):
        return PromptSegmentKind.PROCESSING_STEP
    return PromptSegmentKind.PROCESSING_STEP


def _segment_kind(text: str, source: str) -> PromptSegmentKind:
    kind = _kind(text)
    # An item of an explicit numbered/bulleted list is a deliverable even when
    # its verb is missing from _GOAL_RE ("Chỉ ra 3 ý chính"), unless it reads
    # as preparation ("Đọc file") or a label ("Đối tượng: học sinh").
    if (
        source == "explicit"
        and kind == PromptSegmentKind.PROCESSING_STEP
        and not _PROCESS_RE.match(text)
        and not _SEQUENTIAL_RE.match(text)
        and not _LABEL_ITEM_RE.match(text)
        and len(re.findall(r"\w+", text, re.UNICODE)) >= 3
    ):
        return PromptSegmentKind.GOAL
    return kind


def _confidence(source: str, kind: PromptSegmentKind) -> float:
    if kind != PromptSegmentKind.GOAL:
        return 0.9 if source == "explicit" else 0.75
    return {
        "explicit": 0.96,
        "list_prefix": 0.90,
        "delimiter": 0.88,
        # A plain "and/và" can join either two deliverables or two steps of one
        # deliverable, so leave it below the deterministic threshold.
        "connector": 0.82,
        "prose": 0.90,
    }.get(source, 0.75)


class DeterministicGoalExtractor:
    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()

    def extract(self, prompt: str) -> GoalExtractionResult:
        original = prompt
        normalized = normalize_prompt(prompt)
        sources = _source_spans(prompt)
        raw_segments = _raw_segments(prompt, sources)
        segments: list[PromptSegment] = []
        goals: list[ExplicitGoal] = []
        normalized_goals: set[str] = set()
        duplicate_goal = False
        previous_goal_id: str | None = None

        for raw in raw_segments:
            text = prompt[raw.start : raw.end]
            normalized_text = normalize_prompt(text)
            kind = _segment_kind(normalized_text, raw.source)
            confidence = _confidence(raw.source, kind)
            segment = PromptSegment(
                segment_id=f"segment-{len(segments) + 1}",
                kind=kind,
                text=text,
                normalized_text=normalized_text,
                evidence=EvidenceSpan(start=raw.start, end=raw.end, text=text),
                confidence=confidence,
            )
            segments.append(segment)
            if kind != PromptSegmentKind.GOAL:
                continue

            dedupe_key = re.sub(r"\W+", " ", normalized_text.casefold()).strip()
            if dedupe_key in normalized_goals:
                duplicate_goal = True
                continue
            normalized_goals.add(dedupe_key)
            goal_id = f"goal-{len(goals) + 1}"
            depends_on = (
                [previous_goal_id]
                if previous_goal_id and (raw.sequential or _SEQUENTIAL_RE.search(normalized_text))
                else []
            )
            independently_executable = len(re.findall(r"\w+", normalized_text, re.UNICODE)) >= 2 and not bool(
                _VAGUE_ONLY_RE.fullmatch(normalized_text)
            )
            goals.append(
                ExplicitGoal(
                    goal_id=goal_id,
                    text=text,
                    normalized_text=normalized_text,
                    evidence=segment.evidence,
                    confidence=confidence,
                    independently_executable=independently_executable,
                    depends_on_goal_ids=depends_on,
                )
            )
            previous_goal_id = goal_id

        if not goals:
            span = _trimmed_span(prompt, 0, len(prompt)) or (0, len(prompt))
            text = prompt[span[0] : span[1]]
            fallback = PromptSegment(
                segment_id=f"segment-{len(segments) + 1}",
                kind=PromptSegmentKind.GOAL,
                text=text,
                normalized_text=normalized,
                evidence=EvidenceSpan(start=span[0], end=span[1], text=text),
                confidence=0.40,
            )
            segments.append(fallback)
            goals.append(
                ExplicitGoal(
                    goal_id="goal-1",
                    text=text,
                    normalized_text=normalized,
                    evidence=fallback.evidence,
                    confidence=0.40,
                    independently_executable=False,
                )
            )

        # Record embedded output/style clauses separately while retaining the full goal evidence.
        instruction_text = _masked(prompt, sources)
        for pattern, kind in (
            (_OUTPUT_CONSTRAINT_RE, PromptSegmentKind.OUTPUT_CONSTRAINT),
            (_STYLE_RE, PromptSegmentKind.STYLE_PREFERENCE),
        ):
            for match in pattern.finditer(instruction_text):
                text = prompt[match.start() : match.end()]
                segments.append(
                    PromptSegment(
                        segment_id=f"segment-{len(segments) + 1}",
                        kind=kind,
                        text=text,
                        normalized_text=normalize_prompt(text),
                        evidence=EvidenceSpan(start=match.start(), end=match.end(), text=text),
                        confidence=0.92,
                    )
                )

        for start, end in sources:
            text = prompt[start:end]
            segments.append(
                PromptSegment(
                    segment_id=f"segment-{len(segments) + 1}",
                    kind=PromptSegmentKind.SOURCE_MATERIAL,
                    text=text,
                    normalized_text=normalize_prompt(text),
                    evidence=EvidenceSpan(start=start, end=end, text=text),
                    confidence=0.9,
                )
            )
        source_material = "\n\n".join(prompt[start:end] for start, end in sources) or None

        declared_count = _declared_goal_count(
            [
                segment.normalized_text
                for segment in segments
                if segment.kind not in {PromptSegmentKind.GOAL, PromptSegmentKind.SOURCE_MATERIAL}
            ]
        )

        threshold = self.config.explicit_goal_confidence_threshold
        all_clear = all(goal.confidence >= threshold and goal.independently_executable for goal in goals)
        if declared_count is not None and declared_count != len(goals):
            decision = "unclear"
            reason = (
                f"The request declares {declared_count} tasks but {len(goals)} goal(s) were extracted; "
                "boundaries need review"
            )
        elif len(goals) == 1 and all_clear and not duplicate_goal:
            decision = "single"
            reason = "Exactly one clear deliverable was extracted deterministically"
        elif 2 <= len(goals) <= self.config.max_subtasks and all_clear and not duplicate_goal:
            decision = "multi"
            reason = "Multiple clear, independently executable deliverables were extracted deterministically"
        else:
            decision = "unclear"
            reason = "Goal boundaries are not reliable enough for a deterministic decision"
            if duplicate_goal:
                reason = "Duplicate goal evidence requires classifier review"
            elif len(goals) > self.config.max_subtasks:
                reason = "Explicit goal count exceeds the configured subtask limit"

        overall_confidence = min(goal.confidence for goal in goals)
        return GoalExtractionResult(
            original_prompt=original,
            normalized_prompt=normalized,
            segments=segments,
            goals=goals,
            decision=decision,
            confidence=overall_confidence,
            reason=reason,
            extractor_version=self.config.goal_extractor_version,
            source_material=source_material,
            declared_goal_count=declared_count,
        )
