from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from src.backend_ai.gateway.validation.semantic_constraint_extractor import SemanticConstraintProposal
from src.shared.dtos.gateway import Difficulty, TaskType


class LLMSemanticAnalysis(BaseModel):
    """OpenAI-compatible transport schema; domain bounds are validated afterwards."""

    goal_count: int
    task_types: list[TaskType]
    reasoning_depth: int
    task_independence: int
    dependency_clarity: int
    domain_specialization: int
    output_complexity: int
    ambiguity_level: int
    required_capabilities: list[str]
    explicit_constraints: SemanticConstraintProposal
    explanations: list[str]


class LLMInputFromEntry(BaseModel):
    """One dependency -> consumed-field mapping.

    A plain ``dict[str, str]`` on LLMSubTask would emit an "object with
    additionalProperties" JSON Schema, which OpenAI Structured Outputs
    rejects (same class of issue as the ``set`` fields in
    SemanticConstraintProposal). Using a list of fixed-property objects here
    keeps the transport schema OpenAI-strict; llm_decomposer.py folds this
    list back into a dict for the domain SubTask.input_from.
    """

    dependency_task_id: str
    field: str


class LLMSubTask(BaseModel):
    """Provider-facing task schema without unsupported JSON Schema constraints."""

    task_id: str
    description: str
    task_type: TaskType
    difficulty: Difficulty
    dependencies: list[str]
    required_context: list[str]
    required_capabilities: list[str]
    constraints: SemanticConstraintProposal
    expected_output: str
    goal_ids: list[str]
    input_from: list[LLMInputFromEntry]


class LLMPlannedGoal(BaseModel):
    """One deliverable the planner found. ``quote`` must be copied verbatim
    from the request; planner_grounding rejects a goal it cannot find there."""

    goal_id: str
    quote: str
    depends_on_goal_ids: list[str]


class LLMSourceMaterial(BaseModel):
    """Where the shared passage/data sits in the request, given by its first
    and last words instead of a full copy (cheaper and less error-prone than
    asking the model to reproduce a long passage). Both empty when none."""

    starts_with: str
    ends_with: str


class LLMDecompositionResult(BaseModel):
    """Provider-facing graph schema; the domain model and DAG validator remain authoritative."""

    goals: list[LLMPlannedGoal]
    source_material: LLMSourceMaterial
    original_request_id: str
    decomposition_applied: bool
    subtasks: list[LLMSubTask]
    execution_mode: Literal["single", "parallel", "sequential", "hybrid"]
    preserved_constraints: SemanticConstraintProposal
    decomposition_reason: str


class LLMAggregationResult(BaseModel):
    """Provider-facing schema for LLMAggregator: the model only synthesizes
    the final answer text; which subtasks were used and goal coverage are
    tracked by the caller from the subtasks it fed into the prompt, not by
    the model itself (keeps AggregationValidator authoritative, same
    division of responsibility as decompose's graph validator)."""

    final_answer: str
