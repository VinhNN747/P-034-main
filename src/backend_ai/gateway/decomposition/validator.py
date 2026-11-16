from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from src.backend_ai.gateway.core.config import GatewayConfig
from src.shared.dtos.gateway import (
    DecompositionResult,
    EffectiveConstraints,
    GatewayRequest,
    GoalExtractionResult,
)


class GraphValidationReport(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SemanticCompletenessEvaluator(ABC):
    """Extension point for an LLM judge or domain evaluator; not required for structural validity."""

    @abstractmethod
    async def evaluate(self, request: GatewayRequest, result: DecompositionResult) -> GraphValidationReport:
        raise NotImplementedError


class DecompositionValidator:
    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()

    def validate(
        self,
        request: GatewayRequest,
        result: DecompositionResult,
        constraints: EffectiveConstraints,
        goal_extraction: GoalExtractionResult | None = None,
    ) -> GraphValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        tasks = result.subtasks
        if not tasks:
            return GraphValidationReport(valid=False, errors=["EMPTY_TASK_GRAPH"])
        if len(tasks) > self.config.max_subtasks:
            errors.append("MAX_SUBTASKS_EXCEEDED")

        ids = [task.task_id for task in tasks]
        id_set = set(ids)
        if len(ids) != len(id_set):
            errors.append("DUPLICATE_TASK_ID")
        task_signatures: set[tuple[tuple[str, ...], str]] = set()
        graph: dict[str, list[str]] = {}
        for task in tasks:
            signature = (
                tuple(sorted(task.goal_ids)),
                re.sub(r"\W+", " ", task.description.casefold()).strip(),
            )
            if goal_extraction and signature in task_signatures:
                errors.append(f"DUPLICATE_TASK:{task.task_id}")
            task_signatures.add(signature)
            graph[task.task_id] = task.dependencies
            if task.task_id in task.dependencies:
                errors.append(f"SELF_DEPENDENCY:{task.task_id}")
            missing = set(task.dependencies) - id_set
            if missing:
                errors.append(f"MISSING_DEPENDENCY:{task.task_id}:{sorted(missing)}")
            input_from_not_a_dependency = set(task.input_from) - set(task.dependencies)
            if input_from_not_a_dependency:
                errors.append(f"INPUT_FROM_NOT_A_DEPENDENCY:{task.task_id}:{sorted(input_from_not_a_dependency)}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> bool:
            if task_id in visiting:
                return True
            if task_id in visited:
                return False
            visiting.add(task_id)
            if any(dependency in graph and visit(dependency) for dependency in graph.get(task_id, [])):
                return True
            visiting.remove(task_id)
            visited.add(task_id)
            return False

        if any(visit(task_id) for task_id in ids if task_id not in visited):
            errors.append("DEPENDENCY_CYCLE")

        if result.execution_mode == "single" and (len(tasks) != 1 or tasks[0].dependencies):
            errors.append("INVALID_SINGLE_MODE")
        if result.execution_mode == "parallel" and any(task.dependencies for task in tasks):
            errors.append("PARALLEL_MODE_HAS_DEPENDENCIES")
        if result.execution_mode == "sequential" and len(tasks) > 1:
            for index, task in enumerate(tasks[1:], start=1):
                if not task.dependencies:
                    errors.append(f"ORPHAN_SEQUENTIAL_TASK:{task.task_id}")
                if not set(task.dependencies) & set(ids[:index]):
                    errors.append(f"NON_FORWARD_SEQUENTIAL_DEPENDENCY:{task.task_id}")
        if result.execution_mode == "hybrid" and len(tasks) > 1:
            if not any(task.dependencies for task in tasks):
                errors.append("HYBRID_MODE_HAS_NO_DEPENDENCIES")
            if all(task.dependencies for task in tasks[1:]) and not tasks[0].dependencies:
                warnings.append("HYBRID_GRAPH_MAY_BE_SEQUENTIAL")
        if result.decomposition_applied != (len(tasks) > 1):
            errors.append("DECOMPOSITION_FLAG_TASK_COUNT_MISMATCH")
        if not result.decomposition_applied and result.execution_mode != "single":
            errors.append("SINGLE_PATH_EXECUTION_MODE")

        preserved = result.preserved_constraints
        for key, value in constraints.hard_values().items():
            if key not in preserved or not _same_constraint_value(preserved[key], value):
                errors.append(f"HARD_CONSTRAINT_NOT_PRESERVED:{key}")
            for task in tasks:
                if key not in task.constraints or task.constraints[key] != value:
                    errors.append(f"TASK_CONSTRAINT_NOT_PRESERVED:{task.task_id}:{key}")

        known_goals = {goal.goal_id: goal for goal in goal_extraction.goals} if goal_extraction else {}
        if goal_extraction:
            covered_goals: set[str] = set()
            for task in tasks:
                if not task.goal_ids:
                    errors.append(f"TASK_WITHOUT_GOAL:{task.task_id}")
                    continue
                unknown = set(task.goal_ids) - set(known_goals)
                if unknown:
                    errors.append(f"UNKNOWN_GOAL:{task.task_id}:{sorted(unknown)}")
                covered_goals.update(set(task.goal_ids) & set(known_goals))
                if goal_extraction.decision == "multi" and result.decomposition_applied and len(task.goal_ids) != 1:
                    errors.append(f"MULTI_GOAL_TASK_MERGED:{task.task_id}:{sorted(task.goal_ids)}")

            missing_goals = set(known_goals) - covered_goals
            if missing_goals:
                errors.append(f"UNCOVERED_GOALS:{sorted(missing_goals)}")
            if goal_extraction.decision == "multi" and result.decomposition_applied and len(tasks) < len(known_goals):
                errors.append("INSUFFICIENT_TASKS_FOR_EXPLICIT_GOALS")

        prompt_terms = _content_terms(request.focus_text())
        for task in tasks:
            task_terms = _content_terms(task.description)
            if not goal_extraction and prompt_terms and not prompt_terms.intersection(task_terms):
                errors.append(f"STRUCTURALLY_UNRELATED_TASK:{task.task_id}")
            if goal_extraction and task.goal_ids:
                canonical_task = re.sub(r"\W+", " ", task.description.casefold()).strip()
                mapped_goal_texts = [
                    re.sub(r"\W+", " ", known_goals[goal_id].normalized_text.casefold()).strip()
                    for goal_id in task.goal_ids
                    if goal_id in known_goals
                ]
                goal_terms = {
                    term
                    for goal_id in task.goal_ids
                    if goal_id in known_goals
                    for term in _content_terms(known_goals[goal_id].normalized_text)
                }
                exact_relation = any(
                    goal_text in canonical_task or canonical_task in goal_text
                    for goal_text in mapped_goal_texts
                    if goal_text and canonical_task
                )
                if not exact_relation and (not goal_terms or not goal_terms.intersection(task_terms)):
                    errors.append(f"TASK_UNRELATED_TO_MAPPED_GOAL:{task.task_id}")

        return GraphValidationReport(valid=not errors, errors=errors, warnings=warnings)


_STOP_TERMS = {
    "that", "this", "with", "from", "your", "please",
    "hãy", "được", "những", "các", "của", "cho", "với", "này", "một", "trong", "là", "có", "và",
}  # fmt: skip


def _content_terms(text: str) -> set[str]:
    """Words that carry meaning for the task-to-goal relation check. Vietnamese
    is written in syllables of 2-3 letters ("tóm tắt bài báo"), so a 4-letter
    minimum alone found no terms in a Vietnamese goal and rejected every
    rewritten Vietnamese task; accented syllables of 3+ letters count too."""
    return {
        token
        for token in re.findall(r"\w+", text.casefold(), re.UNICODE)
        if token not in _STOP_TERMS and (len(token) >= 4 or (len(token) >= 3 and not token.isascii()))
    }


def _same_constraint_value(left: Any, right: Any) -> bool:
    """List-valued hard constraints (allowed/blocked models and providers)
    are sets serialized to JSON lists, so their order carries no meaning and
    differs between equal sets. Comparing them in order made validation
    randomly reject valid graphs and fall back to another decomposer."""
    if isinstance(left, list) and isinstance(right, list):
        return sorted(map(str, left)) == sorted(map(str, right))
    return left == right
