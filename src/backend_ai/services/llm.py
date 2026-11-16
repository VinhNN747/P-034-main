from langchain_openai import ChatOpenAI

from src.shared.config import Settings, get_settings
from src.shared.services.provider_credential_resolver import ProviderCredentialResolver


async def get_llm(resolver: ProviderCredentialResolver, settings: Settings | None = None) -> ChatOpenAI:
    resolved_settings = settings or get_settings()
    credential = await resolver.resolve_service("internal_openai")
    return ChatOpenAI(
        model=resolved_settings.model_name,
        api_key=credential.api_key,
        base_url=credential.base_url,
        temperature=resolved_settings.llm_temperature,
        max_retries=0,
    )
