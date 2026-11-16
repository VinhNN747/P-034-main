from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _gemini_standard_pricing() -> tuple[float, float, str]:
    # Google's published standard-tier rates for Gemini 3.x Flash change on
    # 2027-01-01. Keep the defaults aligned after deployment restarts while
    # allowing operators to override every value through environment settings.
    if date.today() < date(2027, 1, 1):
        return 0.75, 3.75, "google-gemini-api-standard-2026"
    return 1.50, 7.50, "google-gemini-api-standard-2027"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "AI20K Agent"
    app_env: Literal["development", "production", "test"] = "development"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_host: str = "0.0.0.0"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001"

    # Transactional email. Keep all provider credentials server-side.
    resend_api_key: str = Field(default="", repr=False)
    resend_from_email: str = ""
    resend_from_name: str = "AI20K Gateway"
    budget_alert_enabled: bool = True

    # Management -> AI calls. Never expose this token to the browser.
    ai_backend_url: str = "http://127.0.0.1:8001"
    ai_service_token: str = Field(default="", repr=False)
    ai_backend_timeout_seconds: float = Field(default=40.0, gt=0, le=600)

    # LLM
    # One shared Fernet key encrypts every provider and internal-service credential.
    credential_encryption_key: str = Field(default="", repr=False)
    model_name: str = "gpt-4o-mini"
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    # Support chatbot understanding and response synthesis. Authorization and data retrieval stay deterministic.
    support_chat_llm_enabled: bool = True
    support_chat_model: str = "gpt-5.6-luna"
    support_chat_llm_timeout_seconds: float = Field(default=75.0, gt=0, le=120)
    support_chat_reasoning_effort: Literal["low", "medium", "high"] = "medium"
    support_chat_answer_max_tokens: int = Field(default=6000, ge=2000, le=12000)
    support_chat_retrieval_chunks: int = Field(default=8, ge=4, le=12)
    support_chat_evidence_chars: int = Field(default=24000, ge=8000, le=48000)
    support_chat_response_temperature: float = Field(default=0.45, ge=0.0, le=1.0)
    support_chat_embedding_model: str = "text-embedding-3-small"
    support_chat_semantic_threshold: float = Field(default=0.35, ge=0, le=1)
    support_chat_intent_threshold: float = Field(default=0.75, ge=0, le=1)
    support_chat_history_messages: int = Field(default=32, ge=2, le=64)
    support_chat_context_chars: int = Field(default=32000, ge=2000, le=64000)
    support_chat_requests_per_minute: int = Field(default=20, ge=1, le=1000)
    support_chat_query_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    support_chat_utc_offset_minutes: int = Field(default=420, ge=-840, le=840)

    # Database
    database_url: str

    # Gateway execution
    gateway_latency_mode: Literal["off", "auto"] = "auto"
    gateway_latency_training_enabled: bool = True
    gateway_latency_history_refresh_seconds: int = Field(default=30, ge=1, le=3600)
    gateway_latency_history_max_age_seconds: int = Field(default=60, ge=1, le=3600)
    gateway_latency_observation_queue_size: int = Field(default=4096, ge=1, le=100_000)
    gateway_latency_writer_batch_size: int = Field(default=100, ge=1, le=1000)
    gateway_latency_writer_flush_seconds: float = Field(default=1.0, gt=0, le=60)
    gateway_latency_writer_retries: int = Field(default=3, ge=0, le=20)
    gateway_latency_training_poll_seconds: int = Field(default=60, ge=1, le=3600)
    gateway_latency_training_interval_seconds: int = Field(default=3600, ge=60, le=86_400)
    gateway_latency_min_training_samples: int = Field(default=500, ge=1)
    gateway_latency_min_new_samples: int = Field(default=200, ge=1)
    gateway_latency_min_profile_train_samples: int = Field(default=50, ge=1)
    gateway_latency_min_profile_validation_samples: int = Field(default=20, ge=1)
    gateway_latency_min_common_validation_samples: int = Field(default=100, ge=1)
    gateway_latency_max_observation_age_seconds: int = Field(default=1800, ge=1)
    gateway_latency_training_window_days: int = Field(default=7, ge=1, le=90)
    gateway_latency_retention_days: int = Field(default=30, ge=1, le=365)
    gateway_latency_max_training_rows: int = Field(default=20_000, ge=100, le=1_000_000)
    gateway_latency_max_features: int = Field(default=256, ge=8, le=4096)
    gateway_latency_max_model_groups: int = Field(default=128, ge=1, le=4096)
    gateway_latency_max_artifact_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    gateway_latency_training_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    gateway_latency_training_max_rss_mb: int = Field(default=512, ge=64, le=16_384)
    gateway_latency_training_max_api_inflight: int = Field(default=1, ge=0, le=10_000)
    gateway_latency_work_dir: str = "data/latency"
    gateway_latency_region: str = "unspecified"
    comparison_worker_enabled: bool = True
    comparison_worker_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    comparison_worker_shutdown_seconds: float = Field(default=30.0, gt=0, le=600)
    gateway_runtime_enabled: bool = False
    gateway_runtime_deadline_seconds: float = Field(default=120.0, ge=1, le=600)
    gateway_runtime_requests_per_minute: int = Field(default=60, ge=1)
    gateway_runtime_max_concurrent_requests: int = Field(default=4, ge=1)
    # Selects the pipeline for the whole process (src/backend_ai/gateway/pipeline_profile.py):
    # "live" (or "online") = Online, "offline" = Offline (no external service is called).
    gateway_execution_mode: Literal["offline", "live"] = "offline"
    # Not read by the runtime: each application's gateway config (database) holds its strategies,
    # and the pipeline maps the ones it does not run. Kept so existing .env files still load.
    gateway_default_router: str = "deterministic"
    gateway_default_aggregator: str = "deterministic"
    gateway_default_understander: str = "rule"
    gateway_default_decomposer: str = "deterministic"
    gateway_default_provider: str = "offline"
    gateway_default_model: str = "local-deterministic"
    gateway_openai_enabled: bool = False
    gateway_gemini_enabled: bool = True
    # Catalog providers of kind "openrouter" (models named "openrouter/<vendor>/<model>").
    gateway_openrouter_enabled: bool = True
    gateway_openrouter_base_url: str = "https://openrouter.ai/api/v1"
    gateway_gemini_model: str = "gemini-3.6-flash"
    gateway_anthropic_enabled: bool = False
    # Fallback only for provider connection probes that omit a model name.
    gateway_anthropic_probe_model: str = Field(
        default="claude-haiku-5-5",
        validation_alias=AliasChoices("GATEWAY_ANTHROPIC_PROBE_MODEL", "GATEWAY_ANTHROPIC_MODEL"),
    )
    gateway_gemini_input_price_per_million_usd: float = Field(
        default_factory=lambda: _gemini_standard_pricing()[0], ge=0
    )
    gateway_gemini_output_price_per_million_usd: float = Field(
        default_factory=lambda: _gemini_standard_pricing()[1], ge=0
    )
    gateway_gemini_pricing_version: str = Field(default_factory=lambda: _gemini_standard_pricing()[2])
    gateway_jev_enabled: bool = False
    gateway_understander_model: str = "gpt-4o-mini"
    gateway_decomposer_model: str = "gpt-4o-mini"
    gateway_aggregator_model: str = "gpt-4o-mini"
    gateway_llm_timeout_seconds: float = Field(default=30.0, gt=0)
    gateway_llm_max_retries: int = Field(default=1, ge=0)
    gateway_model_tier_small_fast: str = "gpt-4o-mini"
    gateway_model_tier_mid: str = "gpt-4o"
    gateway_model_tier_large_reasoning: str = "o1"
    gateway_routing_fallback_model: str = "gpt-4o-mini"
    gateway_routing_timeout_seconds: float = Field(default=30.0, gt=0)
    comparison_evaluation_max_output_tokens: int = Field(default=4096, ge=512, le=32768)
    gateway_pricing_model_id: str = "gpt-4o-mini"
    gateway_pricing_provider: str = "openai"
    gateway_input_price_per_million_usd: float = Field(default=0.15, ge=0)
    gateway_output_price_per_million_usd: float = Field(default=0.60, ge=0)
    gateway_pricing_version: str = "openai-docs-2026-09-21"

    # Multimodal attachments (OpenAI-style image_url / file content parts).
    gateway_ocr_enabled: bool = False
    gateway_ocr_model: str = "gemini-3.8-flash"
    gateway_ocr_timeout_seconds: float = Field(default=60.0, gt=0)
    gateway_ocr_max_output_tokens: int = Field(default=8192, gt=0)
    # Gemini 3.8 Flash shares the published Gemini 3.x Flash standard rates.
    gateway_ocr_input_price_per_million_usd: float = Field(
        default_factory=lambda: _gemini_standard_pricing()[0], ge=0
    )
    gateway_ocr_output_price_per_million_usd: float = Field(
        default_factory=lambda: _gemini_standard_pricing()[1], ge=0
    )
    gateway_attachment_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    gateway_pdf_max_ocr_pages: int = Field(default=50, gt=0)
    gateway_pdf_text_layer_min_chars_per_page: int = Field(default=200, ge=0)

    # Conversation routing window (which messages drive routing decisions)
    gateway_routing_message_countback: int = Field(default=1, ge=1)
    gateway_routing_context_messages: int = Field(default=2, ge=0)
    gateway_routing_context_max_chars: int = Field(default=1500, ge=0)
    # On: jev reads the system prompt (capped), where RAG apps put the documents they answer
    # from; off, a lookup in a provided document was scored like recalling the facts.
    # Note the system prompt text is then sent to the jev provider.
    gateway_routing_include_system_prompt: bool = True
    gateway_routing_system_max_chars: int = Field(default=4000, ge=0)
    # Session affinity: the tier is decided on every turn, but a conversation that stays in
    # the same tier keeps the model that last answered it successfully (warm prompt cache,
    # consistent style). An entry expires this long after its last successful turn.
    gateway_session_affinity_enabled: bool = True
    gateway_session_affinity_ttl_seconds: float = Field(default=1800.0, gt=0)

    # jev scoring + tier policy (jev scores 0..3, the policy picks the tier)
    gateway_jev_model: str = "jev-latest"
    gateway_jev_timeout_seconds: float = Field(default=2.0, gt=0)
    gateway_jev_max_state_chars: int = Field(default=40_000, gt=0)
    # TypeSafe list price (docs.typesafe.ai/models, 2026-10): input only, output free.
    gateway_jev_input_price_per_million_usd: float = Field(default=0.042, ge=0)
    gateway_tier_policy_high: float = Field(default=1.9, ge=0, le=3)
    gateway_tier_policy_very_high: float = Field(default=2.7, ge=0, le=3)
    gateway_tier_policy_min_confidence: float = Field(default=0.0, ge=0, le=1)
    # Log the learned tier model's choice next to the rule decision (no routing effect).
    gateway_tier_shadow_model_enabled: bool = True
    # Local task-type + difficulty classifier (eval/task_type/export_onnx.py output), run in-process
    # with onnxruntime. Unset dir = the "classifier" understander falls back to the rules.
    gateway_task_classifier_dir: str | None = None
    gateway_task_classifier_file: str = "model.onnx"
    gateway_task_classifier_threads: int = Field(default=2, ge=1)
    # onnxruntime's CPU memory arena. Off: ~360 MiB less RAM, slightly slower long prompts.
    gateway_task_classifier_mem_arena: bool = True
    # Or call the model as a service (src/classifier_service/, e.g. on Cloud Run) instead of loading it:
    # the gateway host then needs no model RAM. Takes precedence over the dir. The token is the
    # service's CLASSIFIER_TOKEN. Past the timeout (a cold start) the request routes on the rules.
    gateway_task_classifier_url: str | None = None
    gateway_task_classifier_token: str | None = Field(default=None, repr=False)
    gateway_task_classifier_timeout_seconds: float = Field(default=5.0, gt=0)
    # Production: refuse to start the AI backend when the classifier cannot load, instead of
    # silently routing on the rules.
    gateway_task_classifier_required: bool = False
    # Where src/scripts/fetch_task_classifier.py downloads the model files from (they are not in git):
    # a base URL holding manifest.json + the files, normally a private Hugging Face model repo,
    # https://huggingface.co/<user>/<repo>/resolve/<revision>. The token (a read-only HF token)
    # is sent as a Bearer header.
    gateway_task_classifier_source_url: str | None = None
    gateway_task_classifier_source_token: str | None = Field(default=None, repr=False)
    # Below this task-type probability the classifier's tier is lifted one step (A3).
    gateway_task_classifier_min_confidence: float = Field(default=0.5, ge=0, le=1)
    # Run the classifier next to the selected understander and log it on the understand trace.
    gateway_task_classifier_shadow_enabled: bool = True
    # The "classifier" understander's tier table: difficulty below the first value -> small,
    # below the second -> mid, else large. Per task type overrides as JSON, e.g.
    # GATEWAY_CLASSIFIER_TASK_THRESHOLDS='{"coding": [0.2, 0.5]}'.
    # Defaults come from eval/task_type/calibrate_thresholds.py (re-run after a retrain).
    gateway_classifier_mid_difficulty: float = Field(default=0.23, ge=0, le=1)
    gateway_classifier_large_difficulty: float = Field(default=0.61, ge=0, le=1)
    gateway_classifier_task_thresholds: dict[str, tuple[float, float]] = Field(default_factory=dict)
    # Self-hosted model servers ("local" connections with a base URL). Off on a hosted, multi-tenant
    # deployment: endpoints must then be HTTPS on public addresses (no localhost, LAN or cloud
    # metadata IPs). Turn on for a self-hosted gateway next to its own Ollama / vLLM servers.
    gateway_local_endpoints_allow_private: bool = False
    # Decomposition when goal extraction is "unclear": jev's multi-task probability decides.
    gateway_jev_multi_task_threshold: float = Field(default=0.7, ge=0, le=1)
    gateway_jev_single_task_threshold: float = Field(default=0.3, ge=0, le=1)
    # Split only when parts differ greatly in difficulty: a separate jev call (the split gate)
    # flags it, then each subtask's jev tier must span split_min_tier_gap steps. "legacy" splits
    # whenever several deliverables are found.
    gateway_decomposition_mode: Literal["difficulty_gap", "legacy"] = "difficulty_gap"
    gateway_split_gate_threshold: float = Field(default=0.5, ge=0, le=1)
    gateway_split_min_tier_gap: int = Field(default=2, ge=1, le=2)

    # Files API (OpenAI-compatible /v1/files): bytes in local disk or S3
    # OpenAI-compatible /v1 accepts inline base64 images/files, so its body cap
    # is larger than the 256 KiB runtime API; prefer /v1/files + file_id.
    gateway_file_multipart_max_body_bytes: int = Field(default=52 * 1024 * 1024, gt=0)
    gateway_file_maintenance_enabled: bool = False
    gateway_openai_idempotency_policy: Literal["strict", "best_effort"] = "strict"
    gateway_openai_real_streaming_enabled: bool = False
    gateway_openai_max_body_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    gateway_embeddings_max_body_bytes: int = Field(default=8 * 1024 * 1024, gt=0, le=8 * 1024 * 1024)
    gateway_embeddings_max_result_bytes: int = Field(default=16 * 1024 * 1024, gt=0, le=16 * 1024 * 1024)
    gateway_embeddings_max_batch_size: int = Field(default=128, gt=0, le=128)
    gateway_files_storage: Literal["local", "s3"] = "local"
    gateway_files_local_dir: str = "./data/files"
    gateway_files_s3_bucket: str = ""
    gateway_files_s3_prefix: str = "gateway-files"
    gateway_files_s3_region: str = ""
    gateway_files_s3_endpoint_url: str = ""
    gateway_files_s3_access_key_id: str = ""
    gateway_files_s3_secret_access_key: str = ""
    gateway_files_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    gateway_files_application_quota_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    gateway_files_default_ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)
    gateway_files_min_ttl_seconds: int = Field(default=60 * 60, gt=0)
    gateway_files_max_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, gt=0)

    # Per-model circuit breaker (process-wide)
    gateway_circuit_failure_threshold: int = Field(default=5, ge=1)
    gateway_circuit_window_seconds: float = Field(default=60.0, gt=0)
    gateway_circuit_open_seconds: float = Field(default=30.0, gt=0)
    gateway_circuit_max_open_seconds: float = Field(default=300.0, gt=0)

    # Vector Store
    chroma_persist_dir: str = "./data/chroma"

    # Supabase Auth
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    # Server-only key used to read the owner email through the Supabase Admin API.
    # Prefer SUPABASE_SECRET_KEY for new projects; keep the legacy
    # SUPABASE_SERVICE_ROLE_KEY name supported for existing deployments.
    supabase_service_role_key: str = Field(default="", repr=False)
    supabase_secret_key: str = Field(default="", repr=False)
    supabase_jwt_audience: str = "authenticated"

    @field_validator("gateway_latency_mode", mode="before")
    @classmethod
    def reject_legacy_latency_modes(cls, value: object) -> object:
        if isinstance(value, str) and value in {"observe", "shadow", "active"}:
            raise ValueError(
                "legacy latency modes were removed; set GATEWAY_LATENCY_MODE=auto "
                "to collect/train and use gated predictions, or off to disable latency learning"
            )
        return value


    @field_validator("gateway_execution_mode", mode="before")
    @classmethod
    def _online_means_live(cls, value):
        if isinstance(value, str) and value.strip().lower() == "online":
            return "live"
        return value.strip().lower() if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
