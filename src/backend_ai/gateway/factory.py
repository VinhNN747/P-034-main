from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from time import perf_counter
from typing import Any

from src.backend_ai.agents.routing.base import BaseRouter
from src.backend_ai.agents.routing.deterministic_router import DeterministicRouter
from src.backend_ai.agents.routing.router import ExecutionConfig
from src.backend_ai.gateway.aggregation.base import BaseAggregator
from src.backend_ai.gateway.aggregation.concat_aggregator import ConcatAggregator
from src.backend_ai.gateway.aggregation.llm_aggregator import LLMAggregator
from src.backend_ai.gateway.aggregation.no_aggregator import NoAggregator
from src.backend_ai.gateway.core.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from src.backend_ai.gateway.core.config import GatewayConfig
from src.backend_ai.gateway.decomposition.llm_decomposer import LLMDecomposer
from src.backend_ai.gateway.files.service import FileLimits
from src.backend_ai.gateway.files.storage import FileStorage, LocalFileStorage, S3FileStorage
from src.backend_ai.gateway.llm.client import StructuredModelClient
from src.backend_ai.gateway.multimodal.analyzer import AttachmentAnalyzer, AttachmentLimits
from src.backend_ai.gateway.multimodal.ocr import OcrClient
from src.backend_ai.gateway.pipeline.orchestrator import GatewayOrchestrator
from src.backend_ai.gateway.pipeline.request_pipeline import RequestPipeline
from src.backend_ai.gateway.providers.base import ProviderClient
from src.backend_ai.gateway.providers.offline import OfflineProvider
from src.backend_ai.gateway.routing.learned_tier import LearnedTierModel, LearnedTierPolicy
from src.backend_ai.gateway.routing.tier_policy import TierPolicy, TierPolicyConfig
from src.backend_ai.gateway.understanding.jev import JevUnderstander
from src.backend_ai.gateway.understanding.llm import LLMUnderstander
from src.backend_ai.gateway.understanding.split_gate import SplitGate
from src.backend_ai.gateway.understanding.task_classifier import (
    ClassifierUnderstander,
    OnnxTaskClassifier,
    RemoteTaskClassifier,
)
from src.backend_ai.gateway.validation.semantic_constraint_extractor import LLMConstraintExtractor
from src.backend_ai.services.internal_provider_clients import InternalProviderClients
from src.shared.config import Settings, get_settings
from src.shared.core.pricing import ModelPricing, PricingRegistry
from src.shared.dtos.gateway import RoutingWindow

logger = logging.getLogger(__name__)

class GatewayConfigurationError(RuntimeError):
    """The requested live strategy cannot be assembled from current settings."""


def _resolved_settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


def build_gateway_config(settings: Settings | None = None) -> GatewayConfig:
    resolved = _resolved_settings(settings)
    return GatewayConfig(
        understander_model=resolved.gateway_understander_model,
        decomposer_model=resolved.gateway_decomposer_model,
        aggregator_model=resolved.gateway_aggregator_model,
        llm_timeout_seconds=resolved.gateway_llm_timeout_seconds,
        llm_max_retries=resolved.gateway_llm_max_retries,
        model_tier_small_fast=resolved.gateway_model_tier_small_fast,
        model_tier_mid=resolved.gateway_model_tier_mid,
        model_tier_large_reasoning=resolved.gateway_model_tier_large_reasoning,
        routing_fallback_model=resolved.gateway_routing_fallback_model,
        routing_timeout_seconds=resolved.gateway_routing_timeout_seconds,
        routing_window=RoutingWindow(
            message_countback=resolved.gateway_routing_message_countback,
            context_messages=resolved.gateway_routing_context_messages,
            context_max_chars=resolved.gateway_routing_context_max_chars,
            include_system_prompt=resolved.gateway_routing_include_system_prompt,
            system_max_chars=resolved.gateway_routing_system_max_chars,
        ),
        tier_policy_high=resolved.gateway_tier_policy_high,
        tier_policy_very_high=resolved.gateway_tier_policy_very_high,
        tier_policy_min_confidence=resolved.gateway_tier_policy_min_confidence,
        jev_multi_task_threshold=resolved.gateway_jev_multi_task_threshold,
        jev_single_task_threshold=resolved.gateway_jev_single_task_threshold,
        decomposition_mode=resolved.gateway_decomposition_mode,
        split_gate_threshold=resolved.gateway_split_gate_threshold,
        split_min_tier_gap=resolved.gateway_split_min_tier_gap,
    )


def _tier_policy_config(settings: Settings | None = None) -> TierPolicyConfig:
    resolved = _resolved_settings(settings)
    config = build_gateway_config(resolved)
    return TierPolicyConfig(
        high=config.tier_policy_high,
        very_high=config.tier_policy_very_high,
        min_confidence=config.tier_policy_min_confidence,
        classifier_thresholds=(
            resolved.gateway_classifier_mid_difficulty,
            resolved.gateway_classifier_large_difficulty,
        ),
        classifier_task_thresholds=dict(resolved.gateway_classifier_task_thresholds),
        classifier_min_confidence=resolved.gateway_task_classifier_min_confidence,
    )


def build_tier_policy(settings: Settings | None = None) -> TierPolicy:
    return TierPolicy(_tier_policy_config(settings))


@lru_cache(maxsize=1)
def _learned_tier_model() -> LearnedTierModel | None:
    return LearnedTierModel.load()


def build_shadow_tier_policy(settings: Settings | None = None) -> TierPolicy | None:
    """The learned tier model in shadow mode: its choice is only logged next
    to the rule decision. None when disabled or no weights file is shipped."""
    if not _resolved_settings(settings).gateway_tier_shadow_model_enabled:
        return None
    model = _learned_tier_model()
    return LearnedTierPolicy(model, _tier_policy_config(settings)) if model else None


@lru_cache(maxsize=4)
def _load_task_classifier(
    model_dir: str | None, model_file: str, threads: int, mem_arena: bool = True
) -> tuple[OnnxTaskClassifier | None, str | None]:
    return OnnxTaskClassifier.try_load(model_dir, model_file, threads, mem_arena=mem_arena)


@lru_cache(maxsize=4)
def _remote_task_classifier(url: str, token: str | None, timeout: float) -> RemoteTaskClassifier:
    return RemoteTaskClassifier(url, token, timeout)


def _task_classifier_load(settings: Settings | None = None) -> tuple[OnnxTaskClassifier | None, str | None]:
    resolved = _resolved_settings(settings)
    return _load_task_classifier(
        resolved.gateway_task_classifier_dir,
        resolved.gateway_task_classifier_file,
        resolved.gateway_task_classifier_threads,
        resolved.gateway_task_classifier_mem_arena,
    )


def _uses_remote_classifier(settings: Settings) -> bool:
    # Both pipelines: setting the URL is what selects the service over a local model.
    return bool(settings.gateway_task_classifier_url)


def build_task_classifier(settings: Settings | None = None) -> OnnxTaskClassifier | RemoteTaskClassifier | None:
    """The task classifier: the remote service when GATEWAY_TASK_CLASSIFIER_URL is set (both pipelines),
    else the local model loaded once per process; None when neither is configured."""
    resolved = _resolved_settings(settings)
    if _uses_remote_classifier(resolved):
        return _remote_task_classifier(
            resolved.gateway_task_classifier_url,
            resolved.gateway_task_classifier_token,
            resolved.gateway_task_classifier_timeout_seconds,
        )
    return _task_classifier_load(resolved)[0]


def task_classifier_status(settings: Settings | None = None) -> dict[str, object]:
    """For /health: whether the classifier is configured and loaded, and why not. The remote
    service is not called here: a health check every 30 s would keep Cloud Run from scaling to zero."""
    resolved = _resolved_settings(settings)
    if _uses_remote_classifier(resolved):
        return {
            "configured": True, "mode": "remote", "loaded": True,
            "model": build_task_classifier(resolved).name, "error": None,
        }
    if not resolved.gateway_task_classifier_dir:
        return {"configured": False, "mode": None, "loaded": False, "model": None, "error": None}
    classifier, error = _task_classifier_load(resolved)
    return {
        "configured": True,
        "mode": "local",
        "loaded": classifier is not None,
        "model": classifier.name if classifier else None,
        "error": error,
    }


async def preload_task_classifier(settings: Settings | None = None) -> None:
    """Load the classifier at startup so the first request does not wait for it and a missing
    model shows up in the boot log. Raises when it is required (production) but cannot load."""
    resolved = _resolved_settings(settings)
    if _uses_remote_classifier(resolved):
        return  # nothing to load here; the service loads the model itself
    if not resolved.gateway_task_classifier_dir and not resolved.gateway_task_classifier_required:
        return
    started = perf_counter()
    classifier, error = await asyncio.to_thread(_task_classifier_load, resolved)
    if classifier is None:
        if resolved.gateway_task_classifier_required:
            raise RuntimeError(f"GATEWAY_TASK_CLASSIFIER_REQUIRED is set but the task classifier is disabled: {error}")
        return
    logger.info("task classifier %s loaded in %.1fs", classifier.name, perf_counter() - started)


_CIRCUIT_BREAKER: CircuitBreaker | None = None


def get_circuit_breaker(settings: Settings | None = None) -> CircuitBreaker:
    """Process-wide breaker. Orchestrators are built per request, so the
    breaker must outlive them to remember a model's recent failures."""
    global _CIRCUIT_BREAKER
    if _CIRCUIT_BREAKER is None:
        resolved = _resolved_settings(settings)
        _CIRCUIT_BREAKER = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=resolved.gateway_circuit_failure_threshold,
                window_seconds=resolved.gateway_circuit_window_seconds,
                open_seconds=resolved.gateway_circuit_open_seconds,
                max_open_seconds=resolved.gateway_circuit_max_open_seconds,
            )
        )
    return _CIRCUIT_BREAKER


def register_internal_stage_pricing(registry: PricingRegistry, settings: Settings | None = None) -> PricingRegistry:
    """OCR and jev are gateway-internal stages, not catalog models, so their
    prices are added here unless the catalog already priced them."""
    resolved = _resolved_settings(settings)
    entries = [
        ModelPricing(
            model_id=resolved.gateway_ocr_model,
            provider="google",
            input_price_per_million=resolved.gateway_ocr_input_price_per_million_usd,
            output_price_per_million=resolved.gateway_ocr_output_price_per_million_usd,
            pricing_version="gemini-config",
            capabilities={"vision", "ocr", "pdf"},
            estimated=True,
        ),
        ModelPricing(
            model_id=JevUnderstander.model_id,
            provider="typesafe",
            input_price_per_million=resolved.gateway_jev_input_price_per_million_usd,
            output_price_per_million=0.0,
            pricing_version="typesafe-docs-2026-10",
            capabilities={"classification"},
        ),
    ]
    for entry in entries:
        if registry.get(entry.model_id) is None:
            registry.register(entry)
    return registry


def _openai_capable(settings: Settings) -> bool:
    return (
        settings.gateway_execution_mode == "live"
        and settings.gateway_openai_enabled
    )


def _openrouter_capable(settings: Settings) -> bool:
    return (
        settings.gateway_execution_mode == "live"
        and settings.gateway_openrouter_enabled
    )


def _jev_capable(settings: Settings, internal_clients: InternalProviderClients | None) -> bool:
    return (
        settings.gateway_execution_mode == "live"
        and settings.gateway_jev_enabled
        and internal_clients is not None
        and internal_clients.jev_client is not None
    )


def ensure_strategy_available(
    strategy: str,
    settings: Settings | None = None,
    internal_clients: InternalProviderClients | None = None,
) -> None:
    resolved = _resolved_settings(settings)
    if strategy == "llm" and (
        not _openai_capable(resolved)
        or internal_clients is None
        or internal_clients.llm_client is None
    ):
        raise GatewayConfigurationError(
            "The requested language-model strategy is unavailable because its internal provider binding is not configured."
        )
    # JEV is an optional analysis stage; the pipeline's JevUnderstander uses
    # deterministic rules when its binding is missing or disabled.


def build_attachment_analyzer(
    settings: Settings | None = None,
    *,
    ocr_client: OcrClient | None = None,
) -> AttachmentAnalyzer:
    """Without OCR the analyzer still extracts PDF
    text layers and text files; images are reported as skipped."""
    resolved = _resolved_settings(settings)
    return AttachmentAnalyzer(
        ocr_client,
        circuit_breaker=get_circuit_breaker(resolved),
        limits=AttachmentLimits(
            max_bytes=resolved.gateway_attachment_max_bytes,
            pdf_max_ocr_pages=resolved.gateway_pdf_max_ocr_pages,
            pdf_text_layer_min_chars_per_page=resolved.gateway_pdf_text_layer_min_chars_per_page,
        ),
    )


def build_pipeline(
    *,
    settings: Settings | None = None,
    llm_client: StructuredModelClient | None = None,
    pricing: PricingRegistry | None = None,
    ocr_client: OcrClient | None = None,
    internal_clients: InternalProviderClients | None = None,
) -> RequestPipeline:
    resolved = _resolved_settings(settings)
    config = build_gateway_config(resolved)
    task_classifier = build_task_classifier(resolved)
    pipeline = RequestPipeline(
        config=config,
        tier_policy=build_tier_policy(resolved),
        pricing=register_internal_stage_pricing(pricing or PricingRegistry(), resolved),
        # Local model only: a remote shadow would call the service on every rule-routed request.
        task_classifier_shadow=(
            task_classifier
            if resolved.gateway_task_classifier_shadow_enabled and isinstance(task_classifier, OnnxTaskClassifier)
            else None
        ),
        attachment_analyzer=build_attachment_analyzer(
            resolved,
            ocr_client=ocr_client or (internal_clients.ocr_client if internal_clients else None),
        ),
    )
    if _jev_capable(resolved, internal_clients):
        pipeline.understanders["jev"] = JevUnderstander(
            internal_clients.jev_client,
            timeout_seconds=resolved.gateway_jev_timeout_seconds,
            max_state_chars=resolved.gateway_jev_max_state_chars,
            circuit_breaker=get_circuit_breaker(resolved),
        )
        if internal_clients.split_gate_client is not None:
            pipeline.split_gate = SplitGate(
                internal_clients.split_gate_client,
                timeout_seconds=resolved.gateway_jev_timeout_seconds,
                max_state_chars=resolved.gateway_jev_max_state_chars,
                circuit_breaker=get_circuit_breaker(resolved),
            )
    # Always registered so an application can select it; without a model it runs the rules.
    pipeline.understanders["classifier"] = ClassifierUnderstander(task_classifier)
    client = llm_client or (internal_clients.llm_client if internal_clients else None)
    if client is not None:
        pipeline.register_llm_stages(
            LLMUnderstander(client=client, config=config),
            LLMDecomposer(client=client, config=config),
            LLMConstraintExtractor(client=client, config=config),
        )
    return pipeline


def build_providers(
    settings: Settings | None = None,
    *,
    model_provider_kinds: dict[str, str] | None = None,
) -> dict[str, ProviderClient]:
    resolved = _resolved_settings(settings)
    providers: dict[str, ProviderClient] = {}
    if resolved.gateway_execution_mode == "offline":
        providers["offline"] = OfflineProvider()
    return providers


def build_routers(
    *,
    settings: Settings | None = None,
    database_router: BaseRouter | None = None,
    jev_router: BaseRouter | None = None,
    internal_clients: InternalProviderClients | None = None,
) -> dict[str, BaseRouter]:
    resolved = _resolved_settings(settings)
    if _openai_capable(resolved):
        model_by_tier = {
            "small_fast": resolved.gateway_model_tier_small_fast,
            "mid": resolved.gateway_model_tier_mid,
            "large_reasoning": resolved.gateway_model_tier_large_reasoning,
        }
    else:
        model_by_tier = {
            "small_fast": resolved.gateway_default_model,
            "mid": resolved.gateway_default_model,
            "large_reasoning": resolved.gateway_default_model,
        }
    routers: dict[str, BaseRouter] = {
        "deterministic": DeterministicRouter(
            model_by_tier=model_by_tier,
            fallback_model=resolved.gateway_routing_fallback_model if _openai_capable(resolved) else None,
            policy=build_tier_policy(resolved),
        )
    }
    if database_router is not None:
        routers["database"] = database_router
    if jev_router is not None:
        routers["jev"] = jev_router
    elif internal_clients and internal_clients.legacy_jev_router:
        routers["jev"] = internal_clients.legacy_jev_router
    return routers


def build_aggregators(
    *,
    settings: Settings | None = None,
    llm_client: StructuredModelClient | None = None,
    internal_clients: InternalProviderClients | None = None,
) -> dict[str, BaseAggregator]:
    resolved = _resolved_settings(settings)
    aggregators: dict[str, BaseAggregator] = {
        "none": NoAggregator(),
        "deterministic": ConcatAggregator(),
    }
    client = llm_client or (internal_clients.llm_client if internal_clients else None)
    if client is not None:
        aggregators["llm"] = LLMAggregator(
            client=client,
            config=build_gateway_config(resolved),
            fallback=aggregators["deterministic"],
        )
    return aggregators


def build_orchestrator(
    *,
    settings: Settings | None = None,
    pipeline: RequestPipeline | None = None,
    llm_client: StructuredModelClient | None = None,
    internal_clients: InternalProviderClients | None = None,
    database_router: BaseRouter | None = None,
    jev_router: BaseRouter | None = None,
    providers: dict[str, ProviderClient] | None = None,
    routers: dict[str, BaseRouter] | None = None,
    aggregators: dict[str, BaseAggregator] | None = None,
    persistence: Any | None = None,
    pricing: PricingRegistry | None = None,
    execution_config: ExecutionConfig | None = None,
    circuit_breaker: CircuitBreaker | None = None,
    extra_routers: dict[str, BaseRouter] | None = None,
) -> GatewayOrchestrator:
    resolved = _resolved_settings(settings)
    routers = dict(
        routers
        or build_routers(
            settings=resolved,
            database_router=database_router,
            jev_router=jev_router,
            internal_clients=internal_clients,
        )
    )
    routers.update(extra_routers or {})
    return GatewayOrchestrator(
        pipeline
        or build_pipeline(
            settings=resolved,
            llm_client=llm_client,
            pricing=pricing,
            internal_clients=internal_clients,
        ),
        routers=routers,
        aggregators=aggregators
        or build_aggregators(settings=resolved, llm_client=llm_client, internal_clients=internal_clients),
        providers=providers if providers is not None else build_providers(resolved),
        persistence=persistence,
        execution_config=execution_config,
        circuit_breaker=circuit_breaker or get_circuit_breaker(resolved),
    )


_FILE_STORAGE: FileStorage | None = None


def get_file_storage(settings: Settings | None = None, *, backend=None) -> FileStorage:
    """Process-wide storage backend (an S3 client is expensive to build)."""
    global _FILE_STORAGE
    if backend is not None:
        resolved = _resolved_settings(settings)
        if _FILE_STORAGE is not None and _FILE_STORAGE.name == backend:
            return _FILE_STORAGE
        if backend == "local":
            return LocalFileStorage(resolved.gateway_files_local_dir)
        if backend == "s3":
            return S3FileStorage(resolved.gateway_files_s3_bucket, prefix=resolved.gateway_files_s3_prefix,
                region=resolved.gateway_files_s3_region, endpoint_url=resolved.gateway_files_s3_endpoint_url,
                access_key_id=resolved.gateway_files_s3_access_key_id, secret_access_key=resolved.gateway_files_s3_secret_access_key)
        raise GatewayConfigurationError("Unknown file storage backend")
    if _FILE_STORAGE is None:
        resolved = _resolved_settings(settings)
        if resolved.gateway_files_storage == "s3":
            _FILE_STORAGE = S3FileStorage(
                resolved.gateway_files_s3_bucket,
                prefix=resolved.gateway_files_s3_prefix,
                region=resolved.gateway_files_s3_region,
                endpoint_url=resolved.gateway_files_s3_endpoint_url,
                access_key_id=resolved.gateway_files_s3_access_key_id,
                secret_access_key=resolved.gateway_files_s3_secret_access_key,
            )
        else:
            _FILE_STORAGE = LocalFileStorage(resolved.gateway_files_local_dir)
    return _FILE_STORAGE


def build_file_limits(settings: Settings | None = None) -> FileLimits:
    resolved = _resolved_settings(settings)
    return FileLimits(
        max_file_bytes=resolved.gateway_files_max_bytes,
        application_quota_bytes=resolved.gateway_files_application_quota_bytes,
        default_ttl_seconds=resolved.gateway_files_default_ttl_seconds,
        min_ttl_seconds=resolved.gateway_files_min_ttl_seconds,
        max_ttl_seconds=resolved.gateway_files_max_ttl_seconds,
    )
