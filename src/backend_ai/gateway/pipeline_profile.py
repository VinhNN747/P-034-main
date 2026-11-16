"""The two pipelines the gateway runs, chosen once per process by GATEWAY_EXECUTION_MODE.

Online (``live``): Understand with the LLM / jev / rules / classifier, LLM or deterministic decomposition,
external providers, LLM or deterministic aggregation. Offline (``offline``): no LLM, jev, OCR or external
provider: the task classifier understands, decomposition and aggregation are deterministic, and requests run
on self-hosted endpoints (vLLM / Ollama / OpenAI-compatible) or the offline stub. The classifier is the one
exception: it may run as a service (GATEWAY_TASK_CLASSIFIER_URL, today on Modal, so the gateway host needs no
model RAM), and then the prompt's focus text is sent there. Hosting that service inside the network removes it.

An application stores its strategies (``default_understander``...) in the database, and one application may
be served by either pipeline, so the profile maps a strategy the pipeline does not run onto its internal
counterpart (jev/llm -> classifier, llm -> deterministic) with a warning, instead of failing the request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from src.shared.config import Settings

logger = logging.getLogger(__name__)

PipelineName = Literal["online", "offline"]
STAGES = ("understander", "decomposer", "router", "aggregator")


@dataclass(frozen=True)
class PipelineProfile:
    name: PipelineName
    allowed: dict[str, frozenset[str]]
    substitutes: dict[str, dict[str, str]] = field(default_factory=dict)
    external_services: bool = True
    """Whether the pipeline may call an LLM, jev, OCR or an external provider (the classifier service is separate)."""

    def resolve(self, requested: dict[str, str]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        """The strategies this pipeline runs for the requested ones, and {stage: {requested, effective}} for
        each substitution. Unknown values are kept for the caller to reject."""
        effective = dict(requested)
        substituted: dict[str, dict[str, str]] = {}
        for stage, value in requested.items():
            replacement = self.substitutes.get(stage, {}).get(value)
            if replacement is not None:
                effective[stage] = replacement
                substituted[stage] = {"requested": value, "effective": replacement}
        return effective, substituted


ONLINE = PipelineProfile(
    name="online",
    allowed={
        "understander": frozenset({"rule", "llm", "jev", "classifier"}),
        "decomposer": frozenset({"none", "deterministic", "llm"}),
        "router": frozenset({"deterministic", "database", "jev"}),
        "aggregator": frozenset({"auto", "none", "deterministic", "llm"}),
    },
)

OFFLINE = PipelineProfile(
    name="offline",
    allowed={
        # "rule" stays selectable: it is local, and it is what the classifier falls back to.
        "understander": frozenset({"classifier", "rule"}),
        "decomposer": frozenset({"none", "deterministic"}),
        "router": frozenset({"deterministic", "database"}),
        "aggregator": frozenset({"auto", "none", "deterministic"}),
    },
    substitutes={
        "understander": {"jev": "classifier", "llm": "classifier"},
        "decomposer": {"llm": "deterministic"},
        "router": {"jev": "database"},
        "aggregator": {"llm": "deterministic"},
    },
    external_services=False,
)


def pipeline_profile(settings: Settings) -> PipelineProfile:
    return ONLINE if settings.gateway_execution_mode == "live" else OFFLINE


def settings_for_application(settings: Settings, application, config=None) -> Settings:
    """The settings one request runs with, so one process serves both pipelines at once.

    An offline pipeline (``application.pipeline == "offline"``) runs offline even on an online server. An offline
    server (GATEWAY_EXECUTION_MODE=offline) keeps every request offline: the process setting is a ceiling, never
    raised by an application. The application's classifier thresholds, when set, replace the server defaults.
    The copy lives only for this request; the process settings are never changed.
    """
    update: dict[str, object] = {}
    if getattr(application, "pipeline", "online") == "offline" and settings.gateway_execution_mode != "offline":
        update["gateway_execution_mode"] = "offline"
    if config is not None:
        mid = getattr(config, "classifier_mid_difficulty", None)
        large = getattr(config, "classifier_large_difficulty", None)
        if mid is not None:
            update["gateway_classifier_mid_difficulty"] = mid
        if large is not None:
            update["gateway_classifier_large_difficulty"] = large
    return settings.model_copy(update=update) if update else settings




def configuration_issues(settings: Settings) -> list[tuple[str, str]]:
    """(level, message) for settings that do not fit the selected pipeline; logged at startup and shown on
    /health. Only "error" stops the AI backend (an explicitly required classifier that the pipeline cannot use)."""
    profile = pipeline_profile(settings)
    issues: list[tuple[str, str]] = []
    if profile.name == "offline":
        if settings.gateway_task_classifier_url:
            # The one deliberate exception to "nothing leaves the deployment": the model is not hosted here.
            issues.append(("info", "the task classifier runs as a service at GATEWAY_TASK_CLASSIFIER_URL: the prompt's "
                                   "focus text is sent there (nothing else leaves the offline pipeline); host it "
                                   "inside your network to keep that text in-house"))
        elif not settings.gateway_task_classifier_dir:
            level = "error" if settings.gateway_task_classifier_required else "warning"
            issues.append((level, "the offline pipeline understands with the task classifier but neither "
                                  "GATEWAY_TASK_CLASSIFIER_URL nor GATEWAY_TASK_CLASSIFIER_DIR is set; requests are "
                                  "routed on the rules"))
        ignored = [name for name, on in (
            ("GATEWAY_OPENAI_ENABLED", settings.gateway_openai_enabled),
            ("GATEWAY_JEV_ENABLED", settings.gateway_jev_enabled),
            ("GATEWAY_OCR_ENABLED", settings.gateway_ocr_enabled),
        ) if on]
        if ignored:
            issues.append(("info", f"{', '.join(ignored)} only apply to the online pipeline and are ignored offline"))
    else:
        if not settings.gateway_openai_enabled:
            issues.append(("info", "GATEWAY_OPENAI_ENABLED=false: applications using the llm understander, decomposer "
                                   "or aggregator get STRATEGY_UNAVAILABLE"))
        if not settings.gateway_jev_enabled:
            issues.append(("info", "GATEWAY_JEV_ENABLED=false: the jev understander scores with the rules"))
    return issues


def log_configuration(settings: Settings) -> list[tuple[str, str]]:
    """Log the selected pipeline and its issues; raise on an error-level issue."""
    profile = pipeline_profile(settings)
    issues = configuration_issues(settings)
    logger.info("gateway pipeline: %s (GATEWAY_EXECUTION_MODE=%s)", profile.name, settings.gateway_execution_mode)
    for level, message in issues:
        (logger.warning if level != "info" else logger.info)("gateway %s pipeline: %s", profile.name, message)
    errors = [message for level, message in issues if level == "error"]
    if errors:
        raise RuntimeError(f"invalid {profile.name} pipeline configuration: " + "; ".join(errors))
    return issues
