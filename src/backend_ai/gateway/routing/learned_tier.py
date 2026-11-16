"""Learned tier choice: a logistic regression over jev's scores.

It replaces only TierPolicy's rule *table* (the base tier); confidence, the
caller's objective and the hard floors still apply through TierPolicy, and
R2b stays as a guard (very high risk without live data -> large_reasoning),
so a model trained on a few hundred labels cannot send a contract or dosing
question to a small tier.

The weights live in ``tier_model_weights.json``; they are trained and
exported by ``eval/lmarena/train_tier_model.py`` (sklearn is needed there
only). Inference here is plain Python: ~20 multiply-adds and a softmax.
For now the model runs in shadow mode (logged next to the rule decision),
see DatabaseRoutingService.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from src.backend_ai.gateway.routing.tier_policy import TIER_ORDER, TierPolicy, TierPolicyConfig
from src.shared.dtos.gateway import RoutingSignals, TaskType

WEIGHTS_PATH = Path(__file__).with_name("tier_model_weights.json")

_TASKS = (
    "coding",
    "reasoning",
    "research",
    "content_generation",
    "general",
    "translation",
    "classification",
    "customer_support",
    "summarization",
    "data_extraction",
)
FEATURES = [
    "reasoning",
    "domain",
    "ambiguity",
    "risk",
    "output",
    "knowledge_rarity",
    "live_data",
    "log_input_chars",
    *(f"task:{task}" for task in _TASKS),
]


def feature_vector(signals: RoutingSignals, task_type: TaskType | str | None) -> list[float]:
    task = TaskType(task_type).value if task_type else ""
    return [
        signals.reasoning,
        signals.domain,
        signals.ambiguity,
        signals.risk,
        signals.output,
        signals.knowledge_rarity,
        signals.live_data,
        math.log1p(signals.input_chars),
        *(1.0 if task == name else 0.0 for name in _TASKS),
    ]


@dataclass(frozen=True)
class LearnedTierModel:
    version: str
    coef: list[list[float]]
    intercept: list[float]
    classes: list[str]

    @classmethod
    def load(cls, path: Path = WEIGHTS_PATH) -> LearnedTierModel | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("features") != FEATURES or data.get("classes") != list(TIER_ORDER):
            raise ValueError(f"{path.name} was trained for different features or tiers; retrain it")
        return cls(version=data["version"], coef=data["coef"], intercept=data["intercept"], classes=data["classes"])

    def probabilities(self, signals: RoutingSignals, task_type: TaskType | str | None) -> dict[str, float]:
        x = feature_vector(signals, task_type)
        logits = [b + sum(w * v for w, v in zip(row, x)) for row, b in zip(self.coef, self.intercept)]
        top = max(logits)
        exps = [math.exp(z - top) for z in logits]
        total = sum(exps)
        return {tier: e / total for tier, e in zip(self.classes, exps)}


class LearnedTierPolicy(TierPolicy):
    """TierPolicy with the rule table replaced by ``LearnedTierModel``."""

    def __init__(self, model: LearnedTierModel, config: TierPolicyConfig | None = None) -> None:
        super().__init__(config)
        self.model = model

    def _base_tier(self, s: RoutingSignals, task_type, reasons: list[str]) -> tuple[str, str]:
        probabilities = self.model.probabilities(s, task_type)
        tier = max(probabilities, key=probabilities.get)
        reasons.append(f"model {self.model.version}: " + ", ".join(f"{t}={p:.2f}" for t, p in probabilities.items()))
        if s.risk >= self.config.very_high and s.live_data < self.config.live_data_threshold and tier != TIER_ORDER[-1]:
            reasons.append(f"guard R2b: risk={s.risk:.1f} >= {self.config.very_high}")
            return "ML+R2b", TIER_ORDER[-1]
        return "ML", tier


def load_learned_policy(config: TierPolicyConfig | None = None) -> LearnedTierPolicy | None:
    model = LearnedTierModel.load()
    return LearnedTierPolicy(model, config) if model else None
