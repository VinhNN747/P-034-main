"""Check an LLM task plan against the request text before trusting it.

The planner proposes the goal boundaries. A goal is accepted only when its
quote is found verbatim in the request, so the model cannot invent or
paraphrase a deliverable. The accepted plan is turned into the same
GoalExtractionResult contract the regex extractor produces, so the DAG
validator, executor and aggregator do not care which one drew the boundaries.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.understanding.goal_extractor import looks_like_request, normalize_prompt
from src.shared.dtos.gateway import (
    EvidenceSpan,
    ExplicitGoal,
    GoalExtractionResult,
    PromptSegment,
    PromptSegmentKind,
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_TRAILING_PUNCTUATION_RE = re.compile(r"[^\w\s]+")
PLANNER_GOAL_CONFIDENCE = 0.9


@dataclass
class GroundedPlan:
    goal_extraction: GoalExtractionResult | None
    errors: list[str] = field(default_factory=list)


def _fold_char(char: str) -> str:
    base = "".join(c for c in unicodedata.normalize("NFD", char) if not unicodedata.combining(c))
    base = base.replace("đ", "d").replace("Đ", "D")
    return base if len(base) == 1 else char


def _fold(text: str) -> str:
    """Strip Vietnamese diacritics one character at a time, so offsets in the
    folded text are offsets in the original."""
    return "".join(_fold_char(char) for char in text)


def _occurrences(text: str, quote: str, start: int = 0) -> Iterator[tuple[int, int]]:
    """Spans of ``quote`` in ``text``, ignoring case, whitespace and punctuation
    differences (models often drop a trailing period or join lines). When that
    finds nothing, diacritics are ignored too: models quietly fix a user's typo
    ("bao nhiều" -> "bao nhiêu") even when told to copy verbatim."""
    words = _WORD_RE.findall(quote)
    if not words:
        return
    pattern = re.compile(r"(?<!\w)" + r"\W+".join(map(re.escape, words)) + r"(?!\w)", re.I | re.UNICODE)
    spans = [(m.start(), m.end()) for m in pattern.finditer(text, start)]
    if not spans:
        folded = re.compile(
            r"(?<!\w)" + r"\W+".join(re.escape(_fold(word)) for word in words) + r"(?!\w)", re.I | re.UNICODE
        )
        spans = [(m.start(), m.end()) for m in folded.finditer(_fold(text), start)]
    yield from spans


def _locate_source(prompt: str, source: dict[str, Any], errors: list[str]) -> tuple[int, int] | None:
    starts_with = str(source.get("starts_with") or "").strip()
    ends_with = str(source.get("ends_with") or "").strip()
    if not starts_with:
        return None
    # Opening words such as "Bài báo:" can also appear inside an instruction
    # ("Tóm tắt bài báo dưới đây"); material normally starts on its own line.
    start = min(
        _occurrences(prompt, starts_with),
        key=lambda span: (prompt[prompt.rfind("\n", 0, span[0]) + 1 : span[0]].strip() != "", span[0]),
        default=None,
    )
    if start is None:
        errors.append("SOURCE_MATERIAL_START_NOT_IN_REQUEST")
        return None
    if not ends_with:
        return start[0], len(prompt.rstrip())
    end = None
    for end in _occurrences(prompt, ends_with, start[0]):
        pass  # the last occurrence: a passage can repeat its closing words
    if end is None:
        errors.append("SOURCE_MATERIAL_END_NOT_IN_REQUEST")
        return None
    closing_punctuation = _TRAILING_PUNCTUATION_RE.match(prompt, end[1])
    return start[0], closing_punctuation.end() if closing_punctuation else end[1]


def ground_plan(
    prompt: str,
    planned_goals: list[dict[str, Any]],
    source: dict[str, Any],
    hint: GoalExtractionResult | None,
    config: GatewayConfig,
) -> GroundedPlan:
    errors: list[str] = []
    if not planned_goals:
        return GroundedPlan(None, ["NO_GOALS"])
    if len(planned_goals) > config.max_subtasks:
        errors.append(f"TOO_MANY_GOALS:{len(planned_goals)}>{config.max_subtasks}")

    source_span = _locate_source(prompt, source, errors)
    goal_ids = [str(goal.get("goal_id") or "") for goal in planned_goals]
    if len(set(goal_ids)) != len(goal_ids) or not all(goal_ids):
        errors.append("DUPLICATE_OR_EMPTY_GOAL_ID")

    used: list[tuple[int, int]] = []
    goals: list[ExplicitGoal] = []
    for goal_id, planned in zip(goal_ids, planned_goals, strict=True):
        quote = str(planned.get("quote") or "")
        candidates = list(_occurrences(prompt, quote))
        if not candidates:
            errors.append(f"GOAL_QUOTE_NOT_IN_REQUEST:{goal_id}")
            continue
        outside_source = [
            span for span in candidates if not (source_span and source_span[0] <= span[0] < source_span[1])
        ]
        if not outside_source:
            errors.append(f"GOAL_INSIDE_SOURCE_MATERIAL:{goal_id}")
            continue
        span = next((s for s in outside_source if all(s[1] <= u[0] or s[0] >= u[1] for u in used)), None)
        if span is None:
            errors.append(f"DUPLICATE_GOAL:{goal_id}")
            continue
        used.append(span)
        if len(planned_goals) > 1 and not looks_like_request(prompt, *span):
            errors.append(f"GOAL_IS_BACKGROUND_NOT_A_REQUEST:{goal_id}")
        unknown = set(planned.get("depends_on_goal_ids") or []) - set(goal_ids)
        if unknown:
            errors.append(f"UNKNOWN_GOAL_DEPENDENCY:{goal_id}:{sorted(unknown)}")
        text = prompt[span[0] : span[1]]
        goals.append(
            ExplicitGoal(
                goal_id=goal_id,
                text=text,
                normalized_text=normalize_prompt(text),
                evidence=EvidenceSpan(start=span[0], end=span[1], text=text),
                confidence=PLANNER_GOAL_CONFIDENCE,
                depends_on_goal_ids=[d for d in planned.get("depends_on_goal_ids") or [] if d != goal_id],
            )
        )

    declared = hint.declared_goal_count if hint else None
    if declared is not None and declared != len(planned_goals):
        errors.append(f"DECLARED_TASK_COUNT_MISMATCH:declared={declared}:planned={len(planned_goals)}")
    if errors:
        return GroundedPlan(None, errors)

    segments = [
        PromptSegment(
            segment_id=f"segment-{index}",
            kind=PromptSegmentKind.GOAL,
            text=goal.text,
            normalized_text=goal.normalized_text,
            evidence=goal.evidence,
            confidence=goal.confidence,
        )
        for index, goal in enumerate(goals, 1)
    ]
    source_material = None
    if source_span:
        source_material = prompt[source_span[0] : source_span[1]]
        segments.append(
            PromptSegment(
                segment_id=f"segment-{len(segments) + 1}",
                kind=PromptSegmentKind.SOURCE_MATERIAL,
                text=source_material,
                normalized_text=normalize_prompt(source_material),
                evidence=EvidenceSpan(start=source_span[0], end=source_span[1], text=source_material),
                confidence=PLANNER_GOAL_CONFIDENCE,
            )
        )
    return GroundedPlan(
        GoalExtractionResult(
            original_prompt=prompt,
            normalized_prompt=normalize_prompt(prompt),
            segments=segments,
            goals=goals,
            decision="multi" if len(goals) > 1 else "single",
            confidence=PLANNER_GOAL_CONFIDENCE,
            reason=f"LLM planner found {len(goals)} deliverable(s); every goal quote was located in the request",
            extractor_version=f"{config.goal_extractor_version}+llm-planner",
            source_material=source_material,
            declared_goal_count=declared,
        )
    )
