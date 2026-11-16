from __future__ import annotations

import json

from src.backend_ai.dtos.llm import LLMDecompositionResult
from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.decomposition.base import BaseDecomposer, DecompositionOutcome
from src.backend_ai.gateway.decomposition.deterministic_decomposer import DeterministicGoalDecomposer
from src.backend_ai.gateway.decomposition.no_decomposer import NoDecomposer
from src.backend_ai.gateway.decomposition.planner_grounding import ground_plan
from src.backend_ai.gateway.decomposition.validator import DecompositionValidator
from src.backend_ai.gateway.llm.client import StructuredModelClient
from src.shared.dtos.gateway import (
    DecompositionResult,
    EffectiveConstraints,
    GatewayRequest,
    GoalExtractionResult,
    GraphValidationStatus,
    ModelUsage,
    PromptSegmentKind,
    RequestProfile,
    SemanticAnalysis,
)


class LLMDecomposer(BaseDecomposer):
    """Plan-then-verify decomposition: the model draws the goal boundaries and
    the task DAG, code checks every goal quote against the request
    (planner_grounding) and the DAG (DecompositionValidator). The regex goal
    extraction is only a hint here, and the fallback when the plan never
    passes verification."""

    name = "llm"

    def __init__(
        self,
        client: StructuredModelClient,
        config: GatewayConfig | None = None,
        fallback: BaseDecomposer | None = None,
    ) -> None:
        self.client = client
        self.config = config or GatewayConfig()
        self.fallback = fallback or NoDecomposer()
        self.single = NoDecomposer()
        self.deterministic_fallback = DeterministicGoalDecomposer()
        self.validator = DecompositionValidator(self.config)

    def _planner_prompt(
        self,
        request: GatewayRequest,
        analysis: SemanticAnalysis,
        profile: RequestProfile,
        constraints: EffectiveConstraints,
        user_request: str,
        hint: GoalExtractionResult | None,
    ) -> str:
        max_tasks = self.config.max_subtasks
        candidate_goals = [goal.normalized_text for goal in hint.goals] if hint else []
        candidate_source = (
            next(
                (s.normalized_text[:120] for s in hint.segments if s.kind == PromptSegmentKind.SOURCE_MATERIAL),
                None,
            )
            if hint
            else None
        )
        declared = hint.declared_goal_count if hint else None
        conversation = request.routing_text()
        return (
            "You are the task planner of an AI gateway. Split the USER REQUEST into the deliverables the user "
            "explicitly asked for, then turn them into a task DAG. Each task is executed by a separate model.\n\n"
            "1. GOALS\n"
            "One goal per distinct deliverable the user asked for. A numbered or bulleted item is usually one "
            "deliverable; 'summarize X and translate it' is two. Do not split one deliverable into internal steps "
            "(reading, preparing, checking). Length, format, language and style requirements are not goals. "
            "Introductions such as 'I will give you a passage' are not goals. Several questions about the same "
            "subject that one expert reply answers together are ONE goal, e.g. a patient asking a doctor what a "
            "drug does, where to buy it and who should avoid it, or a word problem that ends with one question. "
            "Greetings, thanks, 'please reply soon' and sentences describing the user's situation or background "
            "are never goals. Default to ONE goal; return several only when the user clearly asks for separate "
            "results: a numbered or bulleted task list, a stated count ('làm 2 việc'), or requests of different "
            "kinds joined by 'and/và' ('summarize X and translate it'). If the request asks for one thing, "
            f"return exactly one goal. At most {max_tasks} goals. When DECLARED TASK COUNT is a number, return "
            "exactly that many goals.\n"
            "goal.quote: the shortest span of the USER REQUEST that states the deliverable, copied character for "
            "character, including its typos (same language and wording, no paraphrase, no translation, no "
            "spelling fixes). Never quote text from the source material.\n\n"
            "2. SOURCE MATERIAL\n"
            "The passage, data, code or document the tasks operate on, as opposed to instructions. "
            "source_material.starts_with: its first 5-10 words verbatim (a header such as 'Đoạn văn:' may be "
            "included); ends_with: its last 5-10 words verbatim. Both empty strings when there is none.\n\n"
            "3. TASKS\n"
            "Exactly one task per goal, with goal_ids = [that goal_id]. Each task runs in isolation: its executor "
            "sees only the task description, expected output, declared dependency outputs and the source material "
            "— never the rest of the request. Write the description in the user's language, make it "
            "self-contained (name the subject, entities and numbers it is about, which other questions in the "
            "request may have introduced), and keep the per-goal limits (word count, item count, format) of that "
            "goal. Refer to "
            "the source material ('the provided passage') instead of copying it. When there is no source material "
            "and the task needs text quoted in the request, include that text in the description. No meta-tasks "
            "(planning, validating, coordinating) unless the user asked for them.\n\n"
            "4. DEPENDENCIES\n"
            "Add a dependency only when a task consumes another task's output (e.g. 'translate the summary'); "
            "mirror it in goal.depends_on_goal_ids and set input_from to what is consumed "
            "(e.g. dependency 'task-1', field 'summary'). Independent tasks have no dependencies. "
            "execution_mode: 'single' for one task, 'parallel' when no task has dependencies, 'sequential' when "
            "each task after the first depends on an earlier one, otherwise 'hybrid'. The graph must be acyclic.\n\n"
            "5. CONSTRAINTS\n"
            "Copy every effective constraint into preserved_constraints and every hard constraint into each "
            "subtask.constraints.\n\n"
            "HEURISTIC HINTS (a regex pass splitting on sentences and verbs; it often over-splits one question "
            "into several and misses questions without a verb. Decide from the request, not from these):\n"
            f"- candidate goals: {json.dumps(candidate_goals, ensure_ascii=False)}\n"
            f"- candidate source material: {json.dumps(candidate_source, ensure_ascii=False)}\n"
            f"DECLARED TASK COUNT: {declared if declared is not None else 'not stated'}\n\n"
            f"REQUEST ID: {request.request_id}\n"
            f"USER REQUEST:\n<<<\n{user_request}\n>>>\n"
            + (
                f"CONVERSATION AND SYSTEM CONTEXT (reference only):\n<<<\n{conversation}\n>>>\n"
                if conversation and conversation != user_request
                else ""
            )
            + f"CONTEXT KEYS: {sorted(request.context)}\n"
            f"SEMANTIC ANALYSIS: {analysis.model_dump_json()}\nPROFILE: {profile.model_dump_json()}\n"
            "EFFECTIVE CONSTRAINTS: "
            f"{json.dumps(constraints.constraints.model_dump(mode='json', exclude_none=True), ensure_ascii=False)}\n"
            f"HARD CONSTRAINTS: {json.dumps(constraints.hard_values(), ensure_ascii=False)}"
        )

    async def decompose(
        self,
        request: GatewayRequest,
        analysis: SemanticAnalysis,
        profile: RequestProfile,
        constraints: EffectiveConstraints,
        reason: str,
        goal_extraction: GoalExtractionResult | None = None,
    ) -> DecompositionOutcome:
        user_request = goal_extraction.original_prompt if goal_extraction else request.focus_text()
        prompt = self._planner_prompt(request, analysis, profile, constraints, user_request, goal_extraction)
        feedback = ""
        usage = ModelUsage()
        errors: list[str] = []
        usage_complete = True
        for _attempt in range(self.config.llm_max_retries + 1):
            try:
                response = await self.client.generate(
                    prompt + feedback,
                    LLMDecompositionResult,
                    self.config.decomposer_model,
                    self.config.llm_timeout_seconds,
                )
                usage.input_tokens += response.usage.input_tokens
                usage.output_tokens += response.usage.output_tokens
                usage_complete = usage_complete and response.usage_complete
                data = dict(response.data)
                grounded = ground_plan(
                    user_request,
                    data.pop("goals", None) or [],
                    data.pop("source_material", None) or {},
                    goal_extraction,
                    self.config,
                )
                planned = grounded.goal_extraction
                if planned is None:
                    raise ValueError(f"Plan grounding failed: {grounded.errors}")

                if len(planned.goals) == 1:
                    # One deliverable: run the request as written instead of a
                    # model-rewritten description that could drop the material.
                    single = await self.single.decompose(
                        request,
                        analysis,
                        profile,
                        constraints,
                        f"{reason}; LLM planner found one deliverable",
                        planned,
                    )
                    single.result.validation_status = GraphValidationStatus.VALID
                    single.strategy = f"{self.name}-planner->single"
                    single.model = response.model
                    single.usage = usage
                    single.usage_complete = usage_complete
                    single.goal_extraction = planned
                    return single

                for task_data in data.get("subtasks", []):
                    task_data["input_from"] = {
                        entry["dependency_task_id"]: entry["field"] for entry in task_data.get("input_from", [])
                    }
                result = DecompositionResult.model_validate(data)
                result.original_request_id = request.request_id
                result.decomposition_applied = len(result.subtasks) > 1
                result.goal_contract_version = planned.extractor_version
                report = self.validator.validate(request, result, constraints, planned)
                if not report.valid:
                    raise ValueError(f"Task graph validation failed: {report.errors}")
                result.validation_status = GraphValidationStatus.VALID
                return DecompositionOutcome(
                    result=result,
                    strategy=self.name,
                    model=response.model,
                    usage=usage,
                    usage_complete=usage_complete,
                    goal_extraction=planned,
                )
            except Exception as exc:
                usage_complete = False
                errors.append(str(exc))
                feedback = f"\nPrevious plan was invalid: {exc}. Correct all listed issues and return the full result."

        fallback_decomposer = (
            self.deterministic_fallback if goal_extraction and goal_extraction.decision == "multi" else self.fallback
        )
        fallback = await fallback_decomposer.decompose(
            request,
            analysis,
            profile,
            constraints,
            reason,
            goal_extraction,
        )
        fallback.result.validation_status = GraphValidationStatus.FALLBACK
        fallback.strategy = f"{self.name}->{fallback_decomposer.name}"
        fallback.model = self.config.decomposer_model
        fallback.fallback = True
        fallback.error = "; ".join(errors)
        fallback.usage = usage
        fallback.usage_complete = usage_complete
        return fallback
