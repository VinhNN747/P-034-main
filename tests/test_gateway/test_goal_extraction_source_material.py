"""Regression suite for "passage + N tasks" prompts.

Before deterministic-goals-v2 the passage was glued to the last list item,
"Chỉ ra 3 ý chính" was not recognized as a goal, and subtasks never saw the
passage (task 1 answered "please provide the passage").
"""

from dataclasses import dataclass

import pytest

from src.backend_ai.agents.routing.base import BaseRouter, RouteDecision
from src.backend_ai.agents.routing.executor import ExecutionContext, SubtaskExecutor
from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.providers.base import ProviderClient, ProviderResponse
from src.backend_ai.gateway.understanding.goal_extractor import DeterministicGoalExtractor
from src.shared.dtos.gateway import Difficulty, GatewayRequest, PromptSegmentKind, SubTask, TaskType

PASSAGE = (
    "Trí tuệ nhân tạo đang ngày càng được ứng dụng rộng rãi trong giáo dục, y tế và kinh doanh. "
    "AI giúp tự động hóa công việc, phân tích dữ liệu nhanh hơn và hỗ trợ con người ra quyết định. "
    "Tuy nhiên, việc lạm dụng AI có thể làm giảm khả năng tư duy độc lập của con người."
)

USER_PROMPT = f"""Tôi sẽ cung cấp cho bạn một đoạn văn.

Hãy thực hiện 3 nhiệm vụ sau:

1. Tóm tắt đoạn văn trong tối đa 50 từ.
2. Chỉ ra 3 ý chính quan trọng nhất.
3. Viết 1 câu kết luận thể hiện thông điệp cốt lõi của đoạn văn.

Đoạn văn:

{PASSAGE}"""


def _extract(prompt: str):
    return DeterministicGoalExtractor().extract(prompt)


def _goal_texts(prompt: str) -> list[str]:
    return [goal.normalized_text for goal in _extract(prompt).goals]


def test_passage_after_numbered_tasks_is_shared_source_not_part_of_last_goal():
    result = _extract(USER_PROMPT)

    assert result.decision == "multi"
    assert result.declared_goal_count == 3
    assert [goal.normalized_text for goal in result.goals] == [
        "Tóm tắt đoạn văn trong tối đa 50 từ.",
        "Chỉ ra 3 ý chính quan trọng nhất.",
        "Viết 1 câu kết luận thể hiện thông điệp cốt lõi của đoạn văn.",
    ]
    assert result.source_material is not None
    assert PASSAGE in result.source_material
    assert all("nhân tạo" not in goal.normalized_text for goal in result.goals)
    assert any(segment.kind == PromptSegmentKind.SOURCE_MATERIAL for segment in result.segments)


def test_passage_before_task_list_stops_at_the_instructions():
    prompt = f"Đoạn văn:\n{PASSAGE}\n\nHãy làm các việc sau:\n1. Tóm tắt đoạn văn.\n2. Dịch đoạn văn sang tiếng Anh."
    result = _extract(prompt)

    assert result.decision == "multi"
    assert _goal_texts(prompt) == ["Tóm tắt đoạn văn.", "Dịch đoạn văn sang tiếng Anh."]
    assert result.source_material is not None
    assert PASSAGE in result.source_material
    assert "Hãy làm" not in result.source_material


def test_unlabeled_passage_after_a_blank_line_is_source_material():
    prompt = f"1. Tóm tắt đoạn văn dưới đây.\n2. Nêu thông điệp chính của tác giả.\n\n{PASSAGE}"
    result = _extract(prompt)

    assert result.decision == "multi"
    assert _goal_texts(prompt) == ["Tóm tắt đoạn văn dưới đây.", "Nêu thông điệp chính của tác giả."]
    assert result.source_material == PASSAGE


def test_numbered_lines_inside_the_passage_are_not_goals():
    passage = "Kế hoạch năm 2025:\n1. Mở rộng thị trường.\n2. Tuyển thêm nhân sự.\n3. Ứng dụng trí tuệ nhân tạo."
    prompt = f"Hãy làm 2 việc:\n- Tóm tắt văn bản.\n- Đánh giá tính khả thi của kế hoạch.\n\nVăn bản:\n{passage}"
    result = _extract(prompt)

    assert result.decision == "multi"
    assert _goal_texts(prompt) == ["Tóm tắt văn bản.", "Đánh giá tính khả thi của kế hoạch."]
    assert result.source_material is not None
    assert "Tuyển thêm nhân sự" in result.source_material


def test_english_tasks_with_text_header():
    prompt = (
        "Do the following 3 tasks:\n"
        "- Summarize the text in two sentences.\n"
        "- Point out the main risks it mentions.\n"
        "- Suggest a short title.\n\n"
        "Text:\nRemote work grew quickly after 2020. It cut commuting time but blurred work-life boundaries."
    )
    result = _extract(prompt)

    assert result.decision == "multi"
    assert result.declared_goal_count == 3
    assert len(result.goals) == 3
    assert result.source_material is not None
    assert "Remote work grew" in result.source_material


@pytest.mark.parametrize(
    "item",
    ["Chỉ ra 3 ý chính quan trọng nhất.", "Nêu 2 ví dụ minh họa.", "Trình bày ưu và nhược điểm."],
)
def test_numbered_items_without_a_known_verb_are_still_goals(item):
    prompt = f"1. Tóm tắt bài viết.\n2. {item}"

    assert _goal_texts(prompt) == ["Tóm tắt bài viết.", item]


def test_label_items_stay_supporting_detail():
    prompt = "Viết email mời họp.\n- Người nhận: phòng kỹ thuật\n- Thời gian: 9h sáng thứ hai"
    result = _extract(prompt)

    assert [goal.normalized_text for goal in result.goals] == ["Viết email mời họp."]


def test_preparation_items_are_not_deliverables():
    prompt = "1. Đọc file data.csv.\n2. Tính trung bình cột giá.\n3. Liệt kê các sản phẩm đắt nhất."

    assert _goal_texts(prompt) == ["Tính trung bình cột giá.", "Liệt kê các sản phẩm đắt nhất."]


def test_declared_count_mismatch_is_unclear():
    prompt = "Hãy làm 3 việc sau: 1. Tóm tắt văn bản. 2. Dịch văn bản sang tiếng Anh."
    result = _extract(prompt)

    assert result.declared_goal_count == 3
    assert len(result.goals) == 2
    assert result.decision == "unclear"
    assert "declares 3 tasks" in result.reason


def test_nhan_tao_is_not_a_create_verb():
    result = _extract("Trí tuệ nhân tạo ảnh hưởng thế nào đến giáo dục? Viết một đoạn ngắn.")

    assert [goal.normalized_text for goal in result.goals] == ["Viết một đoạn ngắn."]


class _PrimaryRouter(BaseRouter):
    name = "primary-only"

    async def aroute(self, subtask, upstream_results=None, **kwargs):
        return RouteDecision(task_id=subtask.task_id, tier="primary", strategy=self.name, selected_model="primary")


@dataclass
class _RecordingProvider(ProviderClient):
    name: str = "recording"

    def __post_init__(self):
        self.prompts: dict[str, str] = {}

    async def generate(self, prompt, *, task_id, model_id, timeout_seconds):
        self.prompts[task_id] = prompt
        return ProviderResponse(text=f"answer:{task_id}", provider=self.name, model_id=model_id)


@pytest.mark.asyncio
async def test_every_subtask_message_carries_the_source_but_not_sibling_tasks():
    provider = _RecordingProvider()
    executor = SubtaskExecutor(_PrimaryRouter(), providers={"primary": provider}, timeout_seconds=2)
    tasks = [
        SubTask(
            task_id=f"task-{index}",
            description=text,
            task_type=TaskType.GENERAL,
            difficulty=Difficulty.EASY,
            expected_output="answer",
        )
        for index, text in enumerate(["Tóm tắt đoạn văn.", "Chỉ ra 3 ý chính."], 1)
    ]

    await executor.aexecute_subtasks(
        tasks, context=ExecutionContext(original_prompt=USER_PROMPT, source_material=PASSAGE)
    )

    assert PASSAGE in provider.prompts["task-1"]
    assert PASSAGE in provider.prompts["task-2"]
    assert "Chỉ ra 3 ý chính" not in provider.prompts["task-1"]
    assert "Tóm tắt đoạn văn" not in provider.prompts["task-2"]


@pytest.mark.asyncio
async def test_orchestrator_splits_user_prompt_and_shares_passage_with_each_task():
    provider = _RecordingProvider()
    orchestrator = GatewayOrchestrator(
        RequestPipeline(config=GatewayConfig(decomposition_mode="legacy")),
        routers={"primary-only": _PrimaryRouter()},
        aggregators={"deterministic": ConcatAggregator()},
        providers={"primary": provider},
    )

    result = await orchestrator.run(
        GatewayRequest(request_id="req-passage", application_id="app-1", prompt=USER_PROMPT),
        understander="rule",
        decomposer="deterministic",
        router="primary-only",
        aggregator="deterministic",
    )

    assert result.decomposition is not None
    assert len(result.decomposition.subtasks) == 3
    assert len(provider.prompts) == 3
    assert all(PASSAGE in prompt for prompt in provider.prompts.values())
    final_answer = result.aggregation.final_answer
    assert "## 1. Tóm tắt đoạn văn trong tối đa 50 từ" in final_answer
    assert "## 2. Chỉ ra 3 ý chính quan trọng nhất" in final_answer
    assert "## 3. Viết 1 câu kết luận" in final_answer
