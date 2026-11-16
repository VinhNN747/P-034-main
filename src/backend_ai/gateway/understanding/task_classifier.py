"""A local encoder classifier (eval/task_type) as an understander: task type + soft difficulty from one ONNX model.

The model runs in-process on CPU (onnxruntime + tokenizers), or in src/classifier_service/ (Cloud Run) when
GATEWAY_TASK_CLASSIFIER_URL is set, so the gateway host needs no model RAM. It replaces the rule
understander entirely: the task type comes from the model, the request is one task (no decomposition), and the tier
comes from the model's difficulty through TierPolicy's classifier table (C1..C3). No regex score is used.

Without the model files or the optional dependencies, or when the model fails, the understander falls back to the
rules for the whole request.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Protocol

from src.backend_ai.gateway.understanding.base import BaseUnderstander, UnderstandingOutcome
from src.backend_ai.gateway.understanding.rules import RuleUnderstander
from src.shared.dtos.gateway import EffectiveConstraints, GatewayRequest, RoutingSignals, SemanticAnalysis, TaskType

logger = logging.getLogger(__name__)

# Capabilities derived from the task type, as in rules.required_capabilities_for_prompt.
_TASK_CAPABILITIES = {TaskType.CODING: "code_generation", TaskType.REASONING: "reasoning"}


@dataclass(frozen=True)
class TaskClassification:
    task_type: TaskType
    probability: float
    difficulty: float
    """Share of the reference 7B pool expected to fail the prompt (0..1)."""
    model: str
    latency_ms: float
    probabilities: dict[str, float] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, object]:
        return {
            "task_type": self.task_type.value,
            "probability": round(self.probability, 4),
            "difficulty": round(self.difficulty, 4),
            "model": self.model,
            "latency_ms": round(self.latency_ms, 1),
        }


class TaskClassifier(Protocol):
    async def aclassify(self, text: str) -> TaskClassification: ...


class OnnxTaskClassifier:
    """model_dir holds labels.json, tokenizer.json and the ONNX file written by eval/task_type/export_onnx.py."""

    def __init__(
        self, model_dir: Path, model_file: str = "model.onnx", threads: int = 2, *, mem_arena: bool = True
    ) -> None:
        from src.backend_ai.gateway.understanding.onnx_inference import OnnxClassifierModel

        self.model = OnnxClassifierModel(model_dir, model_file, threads, mem_arena=mem_arena)
        self.tasks = [TaskType(t) for t in self.model.tasks]
        self.max_len = self.model.max_len
        self.name = self.model.name

    @classmethod
    def load(cls, model_dir: str | Path | None, model_file: str = "model.onnx", threads: int = 2):
        """None (with a warning) when the directory is unset, incomplete, or onnxruntime/tokenizers are missing."""
        return cls.try_load(model_dir, model_file, threads)[0]

    @classmethod
    def try_load(
        cls, model_dir: str | Path | None, model_file: str = "model.onnx", threads: int = 2, *, mem_arena: bool = True
    ) -> tuple[OnnxTaskClassifier | None, str | None]:
        """(classifier, None), or (None, why it is disabled) with a warning logged."""
        from src.backend_ai.gateway.understanding.onnx_inference import OnnxClassifierModel

        if not model_dir:
            return None, "GATEWAY_TASK_CLASSIFIER_DIR is not set"
        path = Path(model_dir)
        missing = OnnxClassifierModel.missing_files(path, model_file)
        if missing:
            reason = f"{', '.join(missing)} missing in {path}"
        else:
            try:
                return cls(path, model_file, threads, mem_arena=mem_arena), None
            except ImportError as exc:
                reason = f"{exc} (install onnxruntime and tokenizers)"
            except Exception as exc:  # a corrupt or truncated model file
                reason = f"cannot load {path / model_file}: {exc}"
        logger.warning("task classifier disabled: %s", reason)
        return None, reason

    def classify(self, text: str) -> TaskClassification:
        prediction = self.model.predict(text)
        return TaskClassification(
            task_type=TaskType(prediction.task),
            probability=prediction.probability,
            difficulty=prediction.difficulty,
            model=self.name,
            latency_ms=prediction.latency_ms,
            probabilities=prediction.probabilities,
        )

    async def aclassify(self, text: str) -> TaskClassification:
        return await asyncio.to_thread(self.classify, text)


class RemoteTaskClassifier:
    """The same model behind src/classifier_service/ (e.g. on Cloud Run), called over HTTPS.

    The prompt's focus text leaves the gateway for that service. A slow or cold service raises
    after ``timeout`` seconds and ClassifierUnderstander routes the request on the rules instead.
    """

    def __init__(self, url: str, token: str | None, timeout: float = 5.0, *, transport=None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._transport = transport
        self._client = None

    @property
    def name(self) -> str:
        return f"remote:{self.url}"

    def _http(self):
        import httpx

        if self._client is None:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            self._client = httpx.AsyncClient(
                headers=headers, timeout=self.timeout, transport=self._transport, follow_redirects=False
            )
        return self._client

    async def aclassify(self, text: str) -> TaskClassification:
        started = perf_counter()
        response = await self._http().post(f"{self.url}/classify", json={"text": text})
        response.raise_for_status()
        body = response.json()
        return TaskClassification(
            task_type=TaskType(body["task_type"]),
            probability=float(body["probability"]),
            difficulty=float(body["difficulty"]),
            model=f"remote:{body.get('model', 'unknown')}",
            latency_ms=(perf_counter() - started) * 1000,
            probabilities={str(k): float(v) for k, v in (body.get("probabilities") or {}).items()},
        )


class ClassifierUnderstander(BaseUnderstander):
    name = "classifier"

    def __init__(self, classifier: TaskClassifier | None, *, fallback: BaseUnderstander | None = None) -> None:
        self.classifier = classifier
        # Used only when the model is missing or fails, never mixed into a classifier result.
        self.fallback = fallback or RuleUnderstander()

    async def understand(self, request: GatewayRequest, constraints: EffectiveConstraints) -> UnderstandingOutcome:
        if self.classifier is None:
            return await self._fall_back(request, constraints, "task classifier is not configured")
        try:
            result = await self.classifier.aclassify(request.focus_text())
        except Exception as exc:  # a broken model must not fail the request
            logger.warning("task classifier failed, using rules: %s", exc)
            return await self._fall_back(request, constraints, f"task classifier failed: {exc}")
        return UnderstandingOutcome(
            analysis=classifier_analysis(result, request, constraints),
            strategy=self.name,
            model=result.model,
            details={"task_classifier": result.as_metadata()},
        )

    async def _fall_back(
        self, request: GatewayRequest, constraints: EffectiveConstraints, error: str
    ) -> UnderstandingOutcome:
        outcome = await self.fallback.understand(request, constraints)
        outcome.fallback = True
        outcome.error = error
        outcome.strategy = f"{self.name}->{self.fallback.name}"
        return outcome


def classifier_analysis(
    result: TaskClassification, request: GatewayRequest, constraints: EffectiveConstraints
) -> SemanticAnalysis:
    """The request as the classifier sees it: one task of the predicted type, routed on its difficulty.

    No regex score or regex task type is used. The scored dimensions are left at 0 because
    nothing measured them; ``TierPolicy`` routes classifier signals on difficulty alone. Only
    measured facts are kept: provided context and the requested output format.
    """
    output_format = constraints.constraints.output_format
    capabilities = [_TASK_CAPABILITIES[result.task_type]] if result.task_type in _TASK_CAPABILITIES else []
    if request.context:
        capabilities.append("long_context")
    if output_format != "text":
        capabilities.append("structured_output")
    return SemanticAnalysis(
        goal_count=1,
        task_types=[result.task_type],
        reasoning_depth=0,
        task_independence=0,
        dependency_clarity=3,
        domain_specialization=0,
        output_complexity=0,
        ambiguity_level=0,
        required_capabilities=capabilities,
        risk_level=0,
        explicit_constraints=constraints.constraints.model_dump(mode="json", exclude_none=True),
        routing_signals=RoutingSignals(
            reasoning=0, domain=0, ambiguity=0, risk=0, output=0,
            source="classifier", difficulty=result.difficulty, confidence=result.probability,
        ),
        explanations=[
            f"Task classifier: {result.task_type.value} (p={result.probability:.2f}), "
            f"difficulty {result.difficulty:.2f}; routed as one task on difficulty."
        ],
    )
