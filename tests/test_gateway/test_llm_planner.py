"""LLM planner: the model draws goal boundaries, code grounds every quote in
the request; regex extraction is only a hint, a cheap gate and the fallback."""

import json
import re

import pytest

from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.decomposition.llm_decomposer import LLMDecomposer
from src.backend_ai.gateway.decomposition.planner_grounding import ground_plan
from src.backend_ai.gateway.llm.client import StructuredModelClient
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.understanding.goal_extractor import DeterministicGoalExtractor
from src.shared.dtos.gateway import GatewayRequest, ModelUsage, StructuredModelResponse

PASSAGE = (
    "Các thành phố lớn đang thử nghiệm xe buýt điện để giảm ô nhiễm không khí. "
    "Chi phí đầu tư ban đầu cao, nhưng chi phí vận hành thấp hơn xe chạy dầu đáng kể."
)
PROSE_PROMPT = f"Tóm tắt bài báo dưới đây và dịch bản tóm tắt sang tiếng Anh.\n\nBài báo:\n{PASSAGE}"


def _constraint_line(prompt: str, label: str) -> dict:
    match = re.search(rf"^{label}: (.*)$", prompt, re.M)
    return json.loads(match.group(1)) if match else {}


def _task(task_id, goal_id, description, prompt, dependencies=()):
    return {
        "task_id": task_id,
        "description": description,
        "task_type": "general",
        "difficulty": "easy",
        "dependencies": list(dependencies),
        "required_context": [],
        "required_capabilities": [],
        "constraints": _constraint_line(prompt, "HARD CONSTRAINTS"),
        "expected_output": description,
        "goal_ids": [goal_id],
        "input_from": [{"dependency_task_id": dep, "field": "summary"} for dep in dependencies],
    }


def _plan(prompt, goals, tasks, source=("", ""), mode="parallel"):
    return {
        "goals": goals,
        "source_material": {"starts_with": source[0], "ends_with": source[1]},
        "original_request_id": "ignored",
        "decomposition_applied": len(tasks) > 1,
        "subtasks": tasks,
        "execution_mode": mode,
        "preserved_constraints": _constraint_line(prompt, "EFFECTIVE CONSTRAINTS"),
        "decomposition_reason": "planned",
    }


class ScriptedPlanner(StructuredModelClient):
    """Returns one scripted plan per call (the last one repeats)."""

    def __init__(self, *builders):
        self.builders = builders
        self.prompts: list[str] = []

    async def generate(self, prompt, schema, model, timeout):
        self.prompts.append(prompt)
        builder = self.builders[min(len(self.prompts), len(self.builders)) - 1]
        return StructuredModelResponse(data=builder(prompt), usage=ModelUsage(input_tokens=10), model=model)


def _summary_then_translation(prompt):
    return _plan(
        prompt,
        goals=[
            {"goal_id": "goal-1", "quote": "Tóm tắt bài báo dưới đây", "depends_on_goal_ids": []},
            {"goal_id": "goal-2", "quote": "dịch bản tóm tắt sang tiếng Anh", "depends_on_goal_ids": ["goal-1"]},
        ],
        tasks=[
            _task("task-1", "goal-1", "Tóm tắt bài báo được cung cấp.", prompt),
            _task("task-2", "goal-2", "Dịch bản tóm tắt sang tiếng Anh.", prompt, dependencies=["task-1"]),
        ],
        source=("Bài báo:", "thấp hơn xe chạy dầu đáng kể."),
        mode="sequential",
    )


def _hallucinated(prompt):
    return _plan(
        prompt,
        goals=[
            {"goal_id": "goal-1", "quote": "Summarize the article", "depends_on_goal_ids": []},
            {"goal_id": "goal-2", "quote": "Translate it", "depends_on_goal_ids": []},
        ],
        tasks=[
            _task("task-1", "goal-1", "Summarize the article.", prompt),
            _task("task-2", "goal-2", "Translate it.", prompt),
        ],
    )


def _pipeline(client):
    # The planner path as such; difficulty_gap mode adds the split gate in front of it.
    return RequestPipeline(config=GatewayConfig(decomposition_mode="legacy"), decomposers={"llm": LLMDecomposer(client)})


async def _run(pipeline, prompt):
    return await pipeline.run(
        GatewayRequest(request_id="req-planner", application_id="app-1", prompt=prompt),
        understander="rule",
        decomposer="llm",
    )


@pytest.mark.asyncio
async def test_planner_splits_prose_request_the_regex_left_unclear():
    assert DeterministicGoalExtractor().extract(PROSE_PROMPT).decision == "unclear"
    client = ScriptedPlanner(_summary_then_translation)

    result = await _run(_pipeline(client), PROSE_PROMPT)

    assert len(client.prompts) == 1
    assert [task.task_id for task in result.decomposition.subtasks] == ["task-1", "task-2"]
    assert result.decomposition.execution_mode == "sequential"
    assert result.goal_extraction.decision == "multi"
    assert result.goal_extraction.extractor_version.endswith("+llm-planner")
    assert [goal.text for goal in result.goal_extraction.goals] == [
        "Tóm tắt bài báo dưới đây",
        "dịch bản tóm tắt sang tiếng Anh",
    ]
    assert PASSAGE in result.goal_extraction.source_material
    decompose_trace = next(trace for trace in result.traces if trace.stage == "decompose")
    assert decompose_trace.metadata["goal_source"] == "llm-planner"


@pytest.mark.asyncio
async def test_hallucinated_quotes_are_retried_then_fall_back_to_regex():
    client = ScriptedPlanner(_hallucinated)

    result = await _run(_pipeline(client), PROSE_PROMPT)

    assert len(client.prompts) == GatewayConfig().llm_max_retries + 1
    assert "Previous plan was invalid" in client.prompts[-1]
    assert "GOAL_QUOTE_NOT_IN_REQUEST" in client.prompts[-1]
    decompose_trace = next(trace for trace in result.traces if trace.stage == "decompose")
    assert decompose_trace.status == "fallback"
    assert decompose_trace.metadata["goal_source"] == "regex"


@pytest.mark.asyncio
async def test_planner_recovers_after_feedback():
    client = ScriptedPlanner(_hallucinated, _summary_then_translation)

    result = await _run(_pipeline(client), PROSE_PROMPT)

    assert len(client.prompts) == 2
    assert len(result.decomposition.subtasks) == 2
    assert result.goal_extraction.extractor_version.endswith("+llm-planner")


@pytest.mark.asyncio
async def test_single_deliverable_plan_runs_the_request_as_written():
    def one_goal(prompt):
        return _plan(
            prompt,
            goals=[{"goal_id": "goal-1", "quote": "phân tích doanh thu quý 3", "depends_on_goal_ids": []}],
            tasks=[_task("task-1", "goal-1", "Phân tích doanh thu.", prompt)],
            mode="single",
        )

    prompt = "Hãy phân tích doanh thu quý 3 và so sánh với quý 2 trong cùng một bảng."
    client = ScriptedPlanner(one_goal)

    result = await _run(_pipeline(client), prompt)

    assert len(client.prompts) == 1
    assert len(result.decomposition.subtasks) == 1
    assert result.decomposition.subtasks[0].description == prompt
    decompose_trace = next(trace for trace in result.traces if trace.stage == "decompose")
    assert decompose_trace.strategy == "llm-planner->single"


@pytest.mark.asyncio
async def test_simple_single_request_skips_the_planner_call():
    client = ScriptedPlanner(_hallucinated)

    result = await _run(_pipeline(client), "Viết email xin nghỉ phép ngày mai.")

    assert client.prompts == []
    assert len(result.decomposition.subtasks) == 1


def _ground(prompt, goals, source=("", ""), hint=None):
    return ground_plan(
        prompt,
        goals,
        {"starts_with": source[0], "ends_with": source[1]},
        hint,
        GatewayConfig(),
    )


def test_quote_matching_ignores_case_whitespace_and_trailing_punctuation():
    prompt = "1. Tóm tắt đoạn văn.\n2. Chỉ ra   3 ý chính."
    grounded = _ground(
        prompt,
        [
            {"goal_id": "g1", "quote": "tóm tắt đoạn văn", "depends_on_goal_ids": []},
            {"goal_id": "g2", "quote": "Chỉ ra 3 ý chính", "depends_on_goal_ids": []},
        ],
    )

    assert grounded.errors == []
    assert [goal.text for goal in grounded.goal_extraction.goals] == ["Tóm tắt đoạn văn", "Chỉ ra   3 ý chính"]


def test_quote_with_a_fixed_typo_still_matches_ignoring_diacritics():
    prompt = "Có bao nhiều cách để xếp 6 cuốn sách lên kệ?"
    grounded = _ground(
        prompt, [{"goal_id": "g1", "quote": "Có bao nhiêu cách để xếp 6 cuốn sách", "depends_on_goal_ids": []}]
    )

    assert grounded.errors == []
    assert grounded.goal_extraction.goals[0].text == "Có bao nhiều cách để xếp 6 cuốn sách"


@pytest.mark.asyncio
async def test_question_without_an_action_verb_skips_the_planner():
    client = ScriptedPlanner(_hallucinated)
    prompt = (
        "Mary có 2 chậu cây trên mỗi giá trong 40 giá đỡ và nhận thêm 18 chậu. "
        "Mỗi giá chỉ để 1 chậu, Mary sẽ còn lại bao nhiêu chậu cây?"
    )
    assert DeterministicGoalExtractor().extract(prompt).decision == "unclear"

    result = await _run(_pipeline(client), prompt)

    assert client.prompts == []
    assert len(result.decomposition.subtasks) == 1


def test_goal_quoted_from_the_source_material_is_rejected():
    prompt = "Tóm tắt văn bản.\n\nVăn bản:\n1. Mở rộng thị trường.\n2. Tuyển thêm nhân sự."
    grounded = _ground(
        prompt,
        [
            {"goal_id": "g1", "quote": "Tóm tắt văn bản", "depends_on_goal_ids": []},
            {"goal_id": "g2", "quote": "Mở rộng thị trường", "depends_on_goal_ids": []},
        ],
        source=("Văn bản:", "Tuyển thêm nhân sự."),
    )

    assert grounded.goal_extraction is None
    assert "GOAL_INSIDE_SOURCE_MATERIAL:g2" in grounded.errors


def test_declared_task_count_must_match_the_plan():
    prompt = "Hãy làm 3 việc sau: 1. Tóm tắt văn bản. 2. Dịch văn bản. 3. Đặt tiêu đề."
    hint = DeterministicGoalExtractor().extract(prompt)
    grounded = _ground(
        prompt,
        [
            {"goal_id": "g1", "quote": "Tóm tắt văn bản", "depends_on_goal_ids": []},
            {"goal_id": "g2", "quote": "Dịch văn bản", "depends_on_goal_ids": []},
        ],
        hint=hint,
    )

    assert any(error.startswith("DECLARED_TASK_COUNT_MISMATCH") for error in grounded.errors)


def test_background_sentence_cannot_be_a_separate_goal():
    prompt = (
        "Tôi bị viêm họng mãn tính, đã đi khám và uống thuốc nhưng không khỏi. "
        "Bác sĩ có phương pháp điều trị dứt điểm không?"
    )
    grounded = _ground(
        prompt,
        [
            {"goal_id": "g1", "quote": "Bác sĩ có phương pháp điều trị dứt điểm không", "depends_on_goal_ids": []},
            {"goal_id": "g2", "quote": "Tôi bị viêm họng mãn tính", "depends_on_goal_ids": []},
        ],
    )

    assert grounded.errors == ["GOAL_IS_BACKGROUND_NOT_A_REQUEST:g2"]


def test_list_items_without_a_verb_are_still_requests():
    prompt = "Về bài báo trên:\n1. Ý chính của tác giả\n2. Giọng văn của bài"
    grounded = _ground(
        prompt,
        [
            {"goal_id": "g1", "quote": "Ý chính của tác giả", "depends_on_goal_ids": []},
            {"goal_id": "g2", "quote": "Giọng văn của bài", "depends_on_goal_ids": []},
        ],
    )

    assert grounded.errors == []


def test_the_same_quote_cannot_be_two_goals():
    grounded = _ground(
        "Viết một đoạn mở bài.",
        [
            {"goal_id": "g1", "quote": "Viết một đoạn mở bài", "depends_on_goal_ids": []},
            {"goal_id": "g2", "quote": "Viết một đoạn mở bài", "depends_on_goal_ids": []},
        ],
    )

    assert "DUPLICATE_GOAL:g2" in grounded.errors
