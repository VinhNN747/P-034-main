"""Build request-scoped clients for gateway-internal provider bindings."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.backend_ai.gateway.llm.client import LangChainOpenAIClient, StructuredModelClient
from src.backend_ai.gateway.multimodal.ocr import GeminiOcrClient, OcrClient
from src.backend_ai.gateway.understanding.jev import JevDecisionClient, TypeSafeJevClient
from src.backend_ai.gateway.understanding.split_gate import SplitGateClient, TypeSafeSplitGateClient
from src.backend_ai.services.support_openai_client import SupportOpenAIClient
from src.shared.config import Settings
from src.shared.services.provider_credential_resolver import (
    CredentialUnavailableError,
    ProviderCredentialResolver,
)


@dataclass(frozen=True, repr=False)
class InternalProviderClients:
    """Resolved clients are intentionally request-scoped and secret-safe to repr."""

    llm_client: StructuredModelClient | None = field(default=None, repr=False)
    ocr_client: OcrClient | None = field(default=None, repr=False)
    jev_client: JevDecisionClient | None = field(default=None, repr=False)
    split_gate_client: SplitGateClient | None = field(default=None, repr=False)
    support_client: SupportOpenAIClient | None = field(default=None, repr=False)
    legacy_jev_router: object | None = field(default=None, repr=False)
    unavailable_purposes: frozenset[str] = frozenset()

    def __repr__(self) -> str:
        return (
            "InternalProviderClients("
            f"llm={'ready' if self.llm_client else 'missing'}, "
            f"ocr={'ready' if self.ocr_client else 'missing'}, "
            f"jev={'ready' if self.jev_client else 'missing'}, "
            f"support={'ready' if self.support_client else 'missing'}, "
            f"unavailable_purposes={sorted(self.unavailable_purposes)!r})"
        )


async def build_internal_clients(
    resolver: ProviderCredentialResolver,
    settings: Settings,
    *,
    need_llm: bool = False,
    need_ocr: bool = False,
    need_jev: bool = False,
    need_support: bool = False,
    need_legacy_router: bool = False,
) -> InternalProviderClients:
    """Resolve only the explicitly requested service bindings.

    Missing bindings are returned as degraded optional capabilities. Callers
    that require a capability (currently the LLM strategy and legacy JEV
    executor) turn that absence into a sanitized strategy-unavailable error.
    """
    llm_credential = None
    jev_credential = None
    ocr_credential = None
    unavailable: set[str] = set()

    live = settings.gateway_execution_mode == "live"
    request_llm = (need_llm and live and settings.gateway_openai_enabled) or (
        need_support and settings.support_chat_llm_enabled
    ) or (need_legacy_router and live)
    request_jev = (need_jev and live and settings.gateway_jev_enabled) or (need_legacy_router and live)
    request_ocr = need_ocr and live and settings.gateway_ocr_enabled

    async def resolve(purpose: str):
        try:
            return await resolver.resolve_service(purpose)
        except CredentialUnavailableError:
            unavailable.add(purpose)
            return None

    if request_llm:
        llm_credential = await resolve("internal_openai")
    if request_jev:
        jev_credential = await resolve("routing_jev")
    if request_ocr:
        ocr_credential = await resolve("ocr_google")

    llm_client = (
        LangChainOpenAIClient(llm_credential.api_key, base_url=llm_credential.base_url)
        if llm_credential and (need_llm or need_legacy_router)
        else None
    )
    support_client = (
        SupportOpenAIClient(
            api_key=llm_credential.api_key,
            model=settings.support_chat_model,
            timeout=settings.support_chat_llm_timeout_seconds,
            base_url=llm_credential.base_url,
        )
        if llm_credential and need_support and settings.support_chat_llm_enabled
        else None
    )
    ocr_client = (
        GeminiOcrClient(
            ocr_credential.api_key,
            model=settings.gateway_ocr_model,
            timeout_seconds=settings.gateway_ocr_timeout_seconds,
            max_output_tokens=settings.gateway_ocr_max_output_tokens,
        )
        if ocr_credential
        else None
    )
    jev_client = (
        TypeSafeJevClient(
            api_key=jev_credential.api_key,
            model=settings.gateway_jev_model,
            timeout_seconds=settings.gateway_jev_timeout_seconds,
        )
        if jev_credential and settings.gateway_jev_enabled
        else None
    )
    split_gate_client = (
        TypeSafeSplitGateClient(
            api_key=jev_credential.api_key,
            model=settings.gateway_jev_model,
            timeout_seconds=settings.gateway_jev_timeout_seconds,
        )
        if jev_credential and settings.gateway_jev_enabled
        else None
    )

    legacy_router = None
    if need_legacy_router and llm_credential and jev_credential:
        # SubtaskRouter's third-party middleware currently requires a short
        # process-environment shim while its TypeSafe classifier is created.
        from src.backend_ai.agents.routing.jev_router import JevRouter
        from src.backend_ai.agents.routing.router import SubtaskRouter

        legacy_router = JevRouter(
            subtask_router=SubtaskRouter(
                settings=settings,
                openai_api_key=llm_credential.api_key,
                openai_base_url=llm_credential.base_url,
                typesafe_api_key=jev_credential.api_key,
            )
        )

    return InternalProviderClients(
        llm_client=llm_client,
        ocr_client=ocr_client,
        jev_client=jev_client,
        split_gate_client=split_gate_client,
        support_client=support_client,
        legacy_jev_router=legacy_router,
        unavailable_purposes=frozenset(unavailable),
    )
