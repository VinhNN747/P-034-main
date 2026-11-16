"""Deterministic tier selection from routing scores.

jev (or the rule/LLM understander) only *scores* the request on a 0..3 scale
per dimension; this table decides the tier. Splitting the two keeps every
routing decision explainable ("rule R2: risk=2.4, domain=2.8") and lets the
thresholds be tuned against a labelled eval set without touching prompts.

Rules are checked top-down and the first match wins. Adjustments then apply
in order: low confidence (safety first), then quality_priority. Floors (R0)
are applied last as hard minimums: they come from measured input (OCR,
length), not from jev, so neither cost_first nor a misleading score can push
a request below them. Every decision keeps the table rule/tier and the list
of adjustments, so logs show what actually moved the tier.

    R1  reasoning >= very_high                              -> large_reasoning
    R1b competition / olympiad / proof (jev yes-no)         -> large_reasoning
    R4c answer in the provided context (jev yes-no) and reasoning < high: rarity
        ignored, risk >= high -> mid (a lookup in a RAG document, policy, manual)
    G   risk >= high but not a conclusion for the user's own case (jev
        yes-no "case_specific") -> mid (R4): R2/R2b need a concrete case
    R2  risk >= r2_min_risk and (domain >= high or ambiguity >= high) -> large_reasoning
    R2b risk >= very_high (legal, medical, safety, big money)   -> large_reasoning
    R3  reasoning + 1.4*domain + output >= mid_score_threshold -> mid
        (several moderate dimensions add up; one high dimension alone,
        especially domain or output, no longer lifts a request),
        or reasoning >= mid_reasoning_alone (math/logic puzzles score high
        on reasoning only, and a small model gets them wrong)
    R3r knowledge_rarity >= high                              -> mid
        (rarity never lifts to large_reasoning: on RouterArena that added
        cost without reducing hard queries routed small_fast)
    R3t easy-looking task small models often fail (jev yes-no) -> mid
    R4  risk >= high                                         -> mid
    L   live data likely (stock/crypto picks, prices, news): R2, R2b, R4 and
        rarity are skipped, since no tier has today's data; reasoning and
        output rules still apply (an analysis still needs a capable model)
    R5  task type is coding / reasoning / research           -> mid
        (rule-sourced scores only; jev scores already cover it)
    R6  otherwise                                            -> small_fast

    Classifier signals (source "classifier", the local BERT task + difficulty model)
    replace the whole R1..R6 table, since their dimension scores are not measured:
    C1  difficulty < classifier_thresholds[0]                -> small_fast
    C2  difficulty < classifier_thresholds[1]                -> mid
    C3  otherwise                                            -> large_reasoning
        (thresholds per task type via classifier_task_thresholds; A3 uses
        classifier_min_confidence on the task-type probability)

    A1  cost_first / latency_first -> one tier down, unless risk >= high
        or reasoning >= very_high (R1 exists because a weaker tier fails)
    A2  quality_first  -> one tier up
    A3  confidence < min_confidence -> one tier up (the scorer was unsure)

    R0a visual attachment (chart, diagram, photo, handwriting) -> at least mid
    R0b attachment OCR could not read                         -> at least mid
    R0c long input (request text + attachment text)           -> at least mid
        (off by default: request size is checked against each model's context
        window when the model is chosen, see DatabaseRoutingService._fit_context)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.shared.core.model_tiers import TIER_ORDER, tier_for_label
from src.shared.dtos.gateway import QualityPriority, RoutingSignals, TaskType

_DEMANDING_TASK_TYPES = frozenset({TaskType.CODING, TaskType.REASONING, TaskType.RESEARCH})


@dataclass(frozen=True)
class TierPolicyConfig:
    # V5 (2026-10-07): tuned on eval/jev_benchmark/cases.jsonl, checked on
    # cases_holdout.jsonl. jev's expected scores run high, so 1.5/2.5 over-routed.
    high: float = 1.9
    """A dimension counts as "high" from here (i.e. level 2 is likely)."""
    very_high: float = 2.7
    """A dimension counts as "very high" from here (level 3 is likely)."""
    min_confidence: float = 0.0
    """A3 off by default: jev is least sure exactly on borderline cases, so a
    confidence bump mostly over-routed them."""
    use_knowledge_rarity: bool = True
    """R3r, the rarity rule (RouterArena, 2026-10-08: short obscure-fact
    questions were routed small_fast while few models know them)."""
    long_input_chars: int = 0
    """R0c floor; 0 disables (default since 2026-10-09). Size is now a capacity check,
    as in LiteLLM's context-window escalation: DatabaseRoutingService drops models whose
    context window cannot hold the request (RoutingSignals.context_chars), instead of
    lifting every long input to mid. Set e.g. 3000 to restore the old floor (RouterArena:
    inputs over 3000 chars were mostly 'hard' yet almost all routed small_fast)."""
    r2_min_risk: float = 2.5
    """R2's risk bar. On 100 hand-labelled real Vietnamese prompts (LMArena,
    2026-10-08) jev scored risk 2.2-3.0 for ordinary money/health/legal
    questions, and R2 at `high` (1.9) sent them to large_reasoning. With rule
    L, 2.5 cut that set's cost ~30% and kept 4/6 truly hard prompts on
    large_reasoning (2.7: 2/6); tuning/holdout/RouterArena did not regress."""
    live_data_threshold: float = 0.6
    """Rule L: jev's probability that the request needs current data at or
    above this skips the risk and rarity rules. Above 1 disables it."""
    mid_score_threshold: float = 5.0
    """R3: reasoning + 1.4*domain + output at or above this -> mid; 0 restores
    the old "any of reasoning/domain/output >= high" rule. On 250 hand-labelled
    real Vietnamese prompts (2026-10-08) that old rule was the main error
    source (22 needless mids, 23 missed mids). The threshold was picked on the
    100 tune prompts and checked on the locked prompts and the local sets."""
    mid_score_weights: tuple[float, float, float] = (1.0, 1.4, 1.0)
    """(reasoning, domain, output) weights for R3, from the learned tier
    model's mid-vs-small coefficients (domain ~1.4x the other two)."""
    mid_reasoning_alone: float = 2.0
    """R3: reasoning at or above this lifts to mid by itself; 0 disables. On
    all 647 LMArena Vietnamese prompts the sum alone sent 32 of 49 math
    prompts to small_fast (old rule 21); with this, 24. No labelled set
    got worse."""
    # The three jev yes/no signals below are None for rule scores and older jev
    # results; each rule then does nothing and routing is as before.
    competition_threshold: float = 0.8
    """R1b: jev's competition/proof probability at or above this ->
    large_reasoning. Above 1 disables. Olympiad prompts scored reasoning < 2.7
    (LiveBench olympiad: 4/10 reached large_reasoning before, 10/10 with R1b).
    0.6 also sent two ordinary math puzzles (p 0.71-0.75) to large_reasoning."""
    case_specific_threshold: float = 0.3
    """R2/R2b gate: with risk >= high but jev's case-specific probability below
    this, the request is general information on a sensitive topic -> mid
    (R4). 0 disables. On the 526-prompt consensus set R2 sent 13 such prompts
    to large_reasoning. Kept low on purpose: 0.5 dropped the one real prompt
    both labellers marked large_reasoning (a contract with a 1bn deposit,
    p=0.38), and a missed large_reasoning costs more than a needless one."""
    answer_in_context_threshold: float = 0.6
    """R4c: jev's answer-in-context probability at or above this, with reasoning
    below `high`, means a lookup in provided documents: rarity is ignored and
    risk lifts only to mid. Above 1 disables. Context benchmark (2026-10-08): a
    question answered by a policy excerpt in the system prompt went to
    large_reasoning exactly like the same question without the excerpt."""
    small_model_trap_threshold: float = 0.6
    """R3t: jev's small-model-trap probability at or above this -> mid. Above 1
    disables. Letter counting, rhymed translation and exact citations scored
    near 0 on every dimension and went to small_fast."""
    # Classifier table (signals.source == "classifier"): the local BERT classifier's
    # difficulty alone picks the tier; the scored rules R1..R6 are not consulted.
    classifier_thresholds: tuple[float, float] = (0.23, 0.61)
    """C1/C2/C3: difficulty below the first value -> small_fast, below the second -> mid,
    else large_reasoning. Difficulty is the expected share of 7B models that fail. The defaults are
    chosen by eval/task_type/calibrate_thresholds.py for the current classifier (not the tier
    boundaries 0.3/0.7 of the true scale: the predicted score is compressed, so those would send only
    35% of hard prompts to large); re-run it after every retrain."""
    classifier_task_thresholds: dict[str, tuple[float, float]] = field(default_factory=dict)
    """Per task type overrides of ``classifier_thresholds``, e.g. {"coding": (0.2, 0.5)}."""
    classifier_min_confidence: float = 0.5
    """A3 for classifier signals: a task-type probability below this lifts one tier."""


@dataclass(frozen=True)
class TierDecision:
    tier: str
    """Final tier."""
    rule: str
    """The rule that set the final tier: the table rule, or R0 when a floor lifted it."""
    reasons: list[str] = field(default_factory=list)
    base_rule: str = ""
    """The table rule (R1..R6) before any adjustment."""
    base_tier: str = ""
    """The tier that rule chose, before confidence, objective and floors."""
    adjustments: list[str] = field(default_factory=list)
    """Each change applied after the table, in order (e.g. 'cost_first: -1')."""

    def explain(self) -> str:
        base = f" (base {self.base_rule} -> {self.base_tier})" if self.base_tier and self.base_tier != self.tier else ""
        return f"{self.rule} -> {self.tier}{base}: " + "; ".join(self.reasons)


def _shift(tier: str, steps: int) -> str:
    index = min(max(TIER_ORDER.index(tier) + steps, 0), len(TIER_ORDER) - 1)
    return TIER_ORDER[index]


class TierPolicy:
    def __init__(self, config: TierPolicyConfig | None = None) -> None:
        self.config = config or TierPolicyConfig()

    def decide(
        self,
        signals: RoutingSignals,
        *,
        task_type: TaskType | str | None = None,
        quality_priority: QualityPriority | str | None = None,
    ) -> TierDecision:
        """Table rule, then confidence (safety first), then the caller's
        objective (with guards), then the hard floors."""
        reasons: list[str] = []
        adjustments: list[str] = []
        base_rule, base_tier = self._base_tier(signals, task_type, reasons)
        tier = self._apply_confidence(base_tier, signals, reasons, adjustments)
        tier = self._apply_objective(tier, signals, quality_priority, reasons, adjustments)
        tier, floored = self._apply_floor(tier, signals, reasons, adjustments)
        return TierDecision(
            tier=tier, rule="R0" if floored else base_rule, reasons=reasons,
            base_rule=base_rule, base_tier=base_tier, adjustments=adjustments,
        )

    def _base_tier(self, s: RoutingSignals, task_type, reasons: list[str]) -> tuple[str, str]:
        if s.source == "classifier" and s.difficulty is not None:
            return self._classifier_tier(s.difficulty, task_type, reasons)
        high, very_high = self.config.high, self.config.very_high
        rarity = s.knowledge_rarity if self.config.use_knowledge_rarity else 0.0
        risk = s.risk
        if s.live_data >= self.config.live_data_threshold:
            # No tier has today's prices or news, so a stronger model only costs more.
            reasons.append(f"live data likely (p={s.live_data:.2f}): risk and rarity rules skipped")
            risk, rarity = 0.0, 0.0
        if s.reasoning >= very_high:
            reasons.append(f"reasoning={s.reasoning:.1f} >= {very_high}")
            return "R1", "large_reasoning"
        if s.competition is not None and s.competition >= self.config.competition_threshold:
            reasons.append(f"competition-level problem (p={s.competition:.2f})")
            return "R1b", "large_reasoning"
        in_context = s.answer_in_context
        if in_context is not None and in_context >= self.config.answer_in_context_threshold and s.reasoning < high:
            # The facts are in the provided documents (RAG, policy, manual): nothing to recall,
            # and a sensitive topic only needs a reliable reader, not the strongest tier.
            rarity = 0.0
            if risk >= high:
                reasons.append(f"risk={risk:.1f}, but the answer is in the provided context "
                               f"(p={in_context:.2f}) and reasoning={s.reasoning:.1f} < {high}")
                return "R4c", "mid"
        gate = self.config.case_specific_threshold
        if risk >= high and gate and s.case_specific is not None and s.case_specific < gate:
            # jev scores risk ~2.6-2.9 for any health/legal/money topic; only a
            # conclusion for the user's own case needs the strongest tier.
            reasons.append(f"risk={risk:.1f} but general information (case-specific p={s.case_specific:.2f})")
            return "R4", "mid"
        if risk >= self.config.r2_min_risk and (s.domain >= high or s.ambiguity >= high):
            reasons.append(f"risk={risk:.1f} with domain={s.domain:.1f}, ambiguity={s.ambiguity:.1f}")
            return "R2", "large_reasoning"
        if risk >= very_high:
            # A serious-consequence request needs the strongest tier even when
            # jev under-scores its domain (e.g. a contract liability clause).
            reasons.append(f"risk={risk:.1f} >= {very_high}")
            return "R2b", "large_reasoning"
        threshold = self.config.mid_score_threshold
        if threshold:
            w_reasoning, w_domain, w_output = self.config.mid_score_weights
            score = w_reasoning * s.reasoning + w_domain * s.domain + w_output * s.output
            alone = self.config.mid_reasoning_alone
            if alone and s.reasoning >= alone:
                reasons.append(f"reasoning={s.reasoning:.1f} >= {alone}")
                return "R3", "mid"
            if score >= threshold:
                reasons.append(
                    f"mid score {score:.1f} >= {threshold} (reasoning={s.reasoning:.1f}, "
                    f"domain={s.domain:.1f}, output={s.output:.1f})"
                )
                return "R3", "mid"
            if rarity >= high:
                reasons.append(f"rarity={rarity:.1f} >= {high}")
                return "R3r", "mid"
        elif s.reasoning >= high or s.domain >= high or s.output >= high or rarity >= high:
            reasons.append(
                f"reasoning={s.reasoning:.1f}, domain={s.domain:.1f}, output={s.output:.1f}, "
                f"rarity={rarity:.1f}; one >= {high}"
            )
            return "R3", "mid"
        trap = s.small_model_trap
        if trap is not None and trap >= self.config.small_model_trap_threshold:
            reasons.append(f"easy-looking task small models often fail (p={trap:.2f})")
            return "R3t", "mid"
        if risk >= high:
            reasons.append(f"risk={risk:.1f} >= {high}")
            return "R4", "mid"
        if task_type in _DEMANDING_TASK_TYPES and s.source != "jev":
            # Only for coarse regex scores: jev already prices code/analysis
            # difficulty into reasoning/output, so R5 would just overrule it.
            reasons.append(f"task_type={TaskType(task_type).value}")
            return "R5", "mid"
        reasons.append("no dimension is high")
        return "R6", "small_fast"

    def _classifier_tier(self, difficulty: float, task_type, reasons: list[str]) -> tuple[str, str]:
        key = TaskType(task_type).value if task_type else None
        mid, large = self.config.classifier_task_thresholds.get(key, self.config.classifier_thresholds)
        label = f"classifier difficulty={difficulty:.2f} ({key or 'any task'})"
        if difficulty >= large:
            reasons.append(f"{label} >= {large}")
            return "C3", "large_reasoning"
        if difficulty >= mid:
            reasons.append(f"{label} >= {mid}")
            return "C2", "mid"
        reasons.append(f"{label} < {mid}")
        return "C1", "small_fast"

    def _apply_confidence(self, tier: str, s: RoutingSignals, reasons: list[str], adjustments: list[str]) -> str:
        bar = self.config.classifier_min_confidence if s.source == "classifier" else self.config.min_confidence
        if s.confidence is None or s.confidence >= bar or tier == TIER_ORDER[-1]:
            return tier
        reasons.append(f"low confidence {s.confidence:.2f} < {bar}: one tier up")
        adjustments.append("A3 low confidence: +1")
        return _shift(tier, 1)

    def _apply_objective(
        self, tier: str, s: RoutingSignals, quality_priority, reasons: list[str], adjustments: list[str]
    ) -> str:
        if quality_priority in (QualityPriority.COST_FIRST, QualityPriority.LATENCY_FIRST):
            # Smaller tiers are both cheaper and faster, so latency_first steps
            # down the same way cost_first does.
            label = QualityPriority(quality_priority).value
            if s.risk >= self.config.high:
                reasons.append(f"{label} ignored: risk is high")
            elif s.reasoning >= self.config.very_high:
                # R1's whole point is that a weaker tier gets the answer wrong.
                reasons.append(f"{label} ignored: reasoning is very high")
            elif tier != TIER_ORDER[0]:
                reasons.append(f"{label}: one tier down")
                adjustments.append(f"A1 {label}: -1")
                return _shift(tier, -1)
        elif quality_priority == QualityPriority.QUALITY_FIRST and tier != TIER_ORDER[-1]:
            reasons.append("quality_first: one tier up")
            adjustments.append("A2 quality_first: +1")
            return _shift(tier, 1)
        return tier

    def _apply_floor(
        self, tier: str, s: RoutingSignals, reasons: list[str], adjustments: list[str]
    ) -> tuple[str, bool]:
        floor_tier, floor_reason = self._floor(s)
        if floor_tier is None or TIER_ORDER.index(tier) >= TIER_ORDER.index(floor_tier):
            return tier, False
        reasons.append(f"{floor_reason}: at least {floor_tier}")
        adjustments.append(f"{floor_reason.split()[0]} floor: {floor_tier}")
        return floor_tier, True

    def _floor(self, s: RoutingSignals) -> tuple[str | None, str]:
        if s.visual_attachments:
            return "mid", f"R0a {s.visual_attachments} visual attachment(s)"
        if s.unreadable_attachments:
            return "mid", f"R0b {s.unreadable_attachments} unreadable attachment(s)"
        limit = self.config.long_input_chars
        if limit and s.input_chars >= limit:
            return "mid", f"R0c long input ({s.input_chars} chars >= {limit})"
        return None, ""


def nearest_tiers(target: str) -> list[str]:
    """Target first, then stronger tiers (safer), then weaker ones."""
    index = TIER_ORDER.index(target)
    return [target, *TIER_ORDER[index + 1 :], *reversed(TIER_ORDER[:index])]


def assign_tiers(items: list[tuple[str, str | None, float]]) -> tuple[dict[str, str], str]:
    """Maps (key, quality_tier label, cost) items to routing tiers.

    Uses the labels when they distinguish at least two tiers; otherwise (an
    unlabelled pool where everything is "standard") ranks by cost: cheapest
    third small_fast, most expensive third large_reasoning. Returns the
    mapping and which source was used, for the route reason.
    """
    labelled = {key: tier_for_label(label) for key, label, _ in items}
    if len({tier for tier in labelled.values() if tier}) >= 2:
        return {key: tier or "mid" for key, tier in labelled.items()}, "quality_tier labels"
    ranked = sorted(items, key=lambda item: item[2])
    if len(ranked) == 1:
        return {ranked[0][0]: "mid"}, "single model"
    mapping = {}
    for position, (key, _, _) in enumerate(ranked):
        share = position / (len(ranked) - 1)
        mapping[key] = "small_fast" if share < 1 / 3 else "large_reasoning" if share > 2 / 3 else "mid"
    return mapping, "price ranking"
