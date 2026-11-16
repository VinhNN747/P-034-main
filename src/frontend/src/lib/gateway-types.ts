export type ApplicationEnvironment = "development" | "staging" | "production";
export type ApplicationStatus = "active" | "paused" | "archived";
export type ApplicationStatusInput = "active" | "paused";
export type ModelStatus = "active" | "inactive";
export type GatewayRequestStatus = "pending" | "completed" | "partial" | "failed";

export type ApiErrorPayload = {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  request_id?: string;
};

export type PageResponse<T> = {
  items: T[];
  page: number;
  page_size: number;
  total: number;
};

export type ApplicationModelSummary = {
  id: string;
  provider?: string;
  name: string;
  display_name?: string;
};

export type ApplicationSummary = {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  owner_user_id: string;
  is_owned_by_current_user?: boolean;
  environment: ApplicationEnvironment;
  status: ApplicationStatus;
  tags?: string[];
  models?: ApplicationModelSummary[];
  routing_objective?: "cost_first" | "balanced" | "latency_first" | "quality_first" | string;
  budget_ceiling?: number;
  request_count?: number;
  config_version?: number;
  model_preview?: ApplicationModelSummary[];
  model_total?: number;
  gateway_config?: GatewayConfig;
  budget?: ApplicationBudgetSummary | null;
  created_at?: string;
  updated_at?: string;
};

export type ApplicationBudgetSummary = {
  application_id: string;
  period_start: string;
  period_end: string;
  limit_usd: number | null;
  spent_usd: number;
  remaining_usd: number | null;
  overage_usd: number;
  usage_percent: number | null;
  warning_threshold: number;
  budget_action: string;
  requests: number;
  tokens: number;
  state: "not_configured" | "healthy" | "warning" | "exhausted";
};

export type ApplicationListParams = {
  scope?: "own" | "global";
  search?: string;
  environment?: ApplicationEnvironment;
  status?: ApplicationStatus;
  page?: number;
  page_size?: number;
  sort_by?: ApplicationSortField | ApplicationSortField[];
  sort_dir?: SortDirection | SortDirection[];
};

export type ApplicationSortField = "name" | "created_at" | "updated_at" | "environment" | "status";

export type ApplicationListResponse = PageResponse<ApplicationSummary>;
export type ApplicationDetail = ApplicationSummary & {
  gateway_config?: GatewayConfig;
  /** "offline" = an offline pipeline (its own section); applications are "online". */
  pipeline?: "online" | "offline";
};

export type GatewayConfigMutationInput = {
  model_pool_id?: string | null;
  routing_policy_id?: string | null;
  budget_ceiling?: number | null;
  context_window?: number | null;
  fallback_model_id?: string | null;
  default_understander?: string;
  default_decomposer?: string;
  default_router?: string;
  default_aggregator?: string;
  model_ids?: string[];
  fallback_model_ids?: string[];
  routing_objective?: string;
  warning_threshold?: number;
  budget_action?: string;
  max_output_tokens?: number;
  max_latency_ms?: number | null;
  timeout_seconds?: number;
  retry_limit?: number;
  fallback_enabled?: boolean;
  expected_version?: number;
};

export type ApplicationCreateInput = {
  slug: string;
  name: string;
  description?: string | null;
  environment?: ApplicationEnvironment;
  status?: ApplicationStatusInput;
  tags?: string[];
  gateway_config?: GatewayConfigMutationInput;
};

export type ApplicationUpdateInput = Partial<
  Pick<ApplicationSummary, "slug" | "name" | "description" | "environment" | "tags"> & { status: ApplicationStatusInput }
>;

export type GatewayConfig = {
  application_id: string;
  model_pool_id: string | null;
  routing_policy_id: string | null;
  version: number;
  routing_objective: string;
  model_ids: string[];
  fallback_model_ids: string[];
  budget_ceiling: number;
  warning_threshold: number;
  budget_action: "block" | "approval" | string;
  max_output_tokens: number;
  max_latency_ms?: number | null;
  timeout_seconds: number;
  retry_limit: number;
  fallback_enabled: boolean;
  understander?: string;
  decomposer?: string;
  router?: string;
  aggregator?: string;
  provider?: string;
  model?: string;
};

export type ModelSummary = {
  id: string;
  provider_id?: string;
  provider: string;
  name: string;
  display_name: string;
  status: ModelStatus;
  capabilities: string[];
  context_window?: number | null;
  max_output_tokens?: number | null;
  target_latency_ms?: number | null;
  last_probe_latency_ms?: number | null;
  feature_metadata?: ModelFeatureMetadata | null;
  quality_tier?: string;
  pricing: {
    input_per_million?: number;
    output_per_million?: number;
    version?: string;
  };
  health_status?: string;
  last_probe_at?: string | null;
  last_probe_credential_version?: number | null;
  used_by_count?: number;
};

export type ModelFeatureSupport = "supported" | "conditional" | "unverified" | "unsupported";
export type ModelFeatureDetail = {
  support: ModelFeatureSupport;
  conditions: string[];
  source_url: string;
};
export type ModelFeatureMetadata = {
  schema_version: 1;
  upstream_features: Record<string, ModelFeatureDetail>;
  gateway_features: Record<string, ModelFeatureDetail>;
  constraints: {
    thinking_mode: "adaptive" | "always_on" | "adaptive_legacy";
    default_effort: "low" | "medium" | "high";
    forced_tool_choice: ModelFeatureSupport;
    custom_sampling: boolean;
    prompt_caching: boolean;
    token_counting: boolean;
  };
  pricing: {
    currency: "USD";
    standard_input_per_million: number;
    standard_output_per_million: number;
    long_context_input_threshold?: number | null;
    long_context_threshold_operator?: ">" | ">=";
    long_context_input_per_million?: number | null;
    long_context_output_per_million?: number | null;
    cache_write_5m_input_per_million: number;
    cache_write_1h_input_per_million: number;
    cache_read_input_per_million: number;
    batch_discount_fraction?: number;
  };
  lifecycle: {
    stage: "active" | "legacy" | "deprecated";
    reliable_knowledge_cutoff?: string | null;
    retirement_commitment?: string | null;
  };
  provenance: {
    checked_on: string;
    verification: "documentation_only" | "live_verified";
    sources: string[];
  };
  latency_reference?: {
    source: "openrouter";
    provider: "Anthropic";
    service_tier: "standard";
    source_metric: "latency_p50";
    metric_interpretation: "ttft";
    latency_ms: number;
    throughput_tokens_per_second: number;
    percentile: 50;
    reasoning_effort: "all";
    locations: "all";
    window: "1 week";
    retrieved_on: string;
    source_url: string;
    verification: "external_reference";
  } | null;
};

export type ModelListParams = {
  provider?: string;
  status?: ModelStatus;
  search?: string;
  page?: number;
  page_size?: number;
  sort_by?: ModelSortField | ModelSortField[];
  sort_dir?: SortDirection | SortDirection[];
};

export type ModelSortField = "name" | "provider" | "status" | "quality_tier" | "context" | "latency" | "input_price" | "output_price" | "used_by";
export type SortDirection = "asc" | "desc";

export type GatewayRequestListParams = {
  application_id?: string;
  search?: string;
  status?: GatewayRequestStatus;
  source?: "playground" | "api" | "unknown";
  page?: number;
  page_size?: number;
  sort_by?: GatewayRequestSortField | GatewayRequestSortField[];
  sort_dir?: SortDirection | SortDirection[];
};

export type GatewayRequestSortField = "started_at" | "latency" | "cost" | "status" | "source";

export type ModelListResponse = PageResponse<ModelSummary>;

export type ApplicationModelsResponse = {
  application_id: string;
  total: number;
  items: ModelSummary[];
};
export type ModelPoolMemberRole = "primary" | "fallback";

export type ModelPoolMemberInput = {
  model_id: string;
  role?: ModelPoolMemberRole;
};

export type ModelPool = {
  id: string;
  owner_user_id: string | null;
  name: string;
  purpose: string;
  status: ModelStatus;
  members: Array<ModelPoolMemberInput & { id: string; role: ModelPoolMemberRole }>;
  warnings?: string[];
  created_at: string;
  updated_at: string;
};

export type FallbackSuggestion = {
  for_model_id: string;
  tier: string;
  model_id: string | null;
  reason: string;
  output_price_delta_per_million: number | null;
};

export type ModelPoolMutationInput = {
  name: string;
  purpose: string;
  status: ModelStatus;
  members: ModelPoolMemberInput[];
};

export type ProviderSummary = {
  id: string;
  name: string;
  display_name: string;
  kind: string;
  status: "active" | "inactive";
};

export type ProviderKind = "openai" | "google" | "openrouter" | "anthropic" | "typesafe" | "offline" | "local";
export type ProviderPurpose = "target" | "internal";
export type CredentialStatus = "active" | "revoked";
export type CredentialTestStatus = "untested" | "healthy" | "failed" | "unavailable" | "skipped";

export type CredentialSummary = {
  configured: boolean;
  status?: CredentialStatus | null;
  version?: number | null;
  key_last4?: string | null;
  last_test_status?: CredentialTestStatus;
  last_test_at?: string | null;
  last_test_credential_version?: number | null;
  last_test_mode?: string | null;
  last_test_model?: string | null;
  last_test_error_code?: string | null;
  last_test_latency_ms?: number | null;
};

export type ProviderConnection = {
  id: string;
  name: string;
  display_name: string;
  kind: ProviderKind;
  purpose: ProviderPurpose;
  status: "active" | "inactive";
  base_url?: string | null;
  credential: CredentialSummary;
  created_at: string;
  updated_at: string;
};

export type EndpointQualityTier = "simple" | "mid" | "hard";

/** A model served by a customer's own endpoint (bring your own server). */
export type EndpointModel = {
  id: string;
  name: string;
  display_name: string;
  quality_tier: string;
  context_window: number | null;
  capabilities: string[];
  status: "active" | "inactive";
  health_status: string;
  last_probe_at: string | null;
  last_probe_latency_ms: number | null;
  last_probe_error: string | null;
};

/** A customer's own OpenAI-compatible server (vLLM, Ollama, ...). */
export type CustomerEndpoint = {
  id: string;
  display_name: string;
  base_url: string;
  status: "active" | "inactive";
  has_api_key: boolean;
  key_last4: string | null;
  models: EndpointModel[];
  created_at: string;
  updated_at: string;
};

export type CustomerEndpointCreateInput = { display_name: string; base_url: string; api_key?: string };

/** Offline pipeline: three self-hosted models behind its own off_ key, routed by the task classifier. */
export type OfflineLadder = Record<EndpointQualityTier, string>;

export type OfflinePipelineModel = {
  tier: EndpointQualityTier;
  model_id: string;
  name: string;
  display_name: string;
  endpoint_id: string;
  endpoint_name: string;
  status: string;
  health_status: string;
  context_window: number | null;
};

export type OfflinePipeline = {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  status: "active" | "paused" | "archived";
  models: OfflinePipelineModel[];
  classifier_mid_difficulty: number;
  classifier_large_difficulty: number;
  thresholds_default: boolean;
  max_output_tokens: number;
  timeout_seconds: number;
  fallback_enabled: boolean;
  request_count: number;
  config_version: number | null;
  created_at: string;
  updated_at: string;
};

export type OfflinePipelineCreateInput = {
  slug: string;
  name: string;
  description?: string | null;
  models: OfflineLadder;
  classifier_mid_difficulty?: number | null;
  classifier_large_difficulty?: number | null;
  max_output_tokens?: number;
  timeout_seconds?: number;
  fallback_enabled?: boolean;
};

export type OfflinePipelineUpdateInput = Partial<Omit<OfflinePipelineCreateInput, "slug">> & { status?: "active" | "paused" };

export type EndpointModelCreateInput = {
  name: string;
  display_name?: string;
  quality_tier: EndpointQualityTier;
  context_window?: number;
  capabilities?: Array<"code_generation" | "reasoning" | "long_context" | "structured_output">;
};

export type ProviderConnectionCreateInput = {
  name: string;
  display_name: string;
  kind: ProviderKind;
  purpose: ProviderPurpose;
  status?: "active" | "inactive";
  base_url?: string | null;
  api_key?: string;
};

export type ProviderConnectionUpdateInput = {
  display_name?: string;
  status?: "active" | "inactive";
};

export type ProviderCredentialReplaceInput = { api_key: string; expected_version: number };
export type ProbeMode = "chat" | "embedding" | "ocr" | "jev";
export type ProviderConnectionProbeInput = { expected_version: number; mode: ProbeMode; model_name?: string };
export type ProviderConnectionProbeResult = {
  provider_id: string;
  credential_version: number | null;
  status: "healthy" | "failed" | "unavailable" | "skipped";
  mode: ProbeMode;
  model_name?: string | null;
  checked_at: string;
  latency_ms: number;
  error_code?: string | null;
  error_message?: string | null;
};

export type ServiceBindingPurpose = "internal_openai" | "ocr_google" | "routing_jev";
export type ServiceProviderBinding = {
  purpose: ServiceBindingPurpose;
  provider_id: string | null;
  provider_name?: string | null;
  provider_kind?: ProviderKind | null;
  provider_status?: "active" | "inactive" | null;
  credential?: CredentialSummary | null;
};
export type ServiceProviderBindingUpdateInput = { provider_id: string | null };

export type ModelCreateInput = {
  provider_id: string;
  name: string;
  display_name: string;
  status?: ModelStatus;
  capabilities?: string[];
  context_window?: number | null;
  max_output_tokens?: number | null;
  feature_metadata?: ModelFeatureMetadata | null;
  target_latency_ms?: number | null;
  input_price_per_million?: number;
  output_price_per_million?: number;
  pricing_version?: string;
  quality_tier?: string;
};

export type ModelUpdateInput = Partial<Pick<ModelCreateInput,
  "display_name" | "status" | "capabilities" | "context_window" | "target_latency_ms" |
  "max_output_tokens" | "feature_metadata" | "input_price_per_million" | "output_price_per_million" | "pricing_version" | "quality_tier"
>>;

export type ModelProbeResponse = {
  model_id: string;
  status: string;
  latency_ms: number;
  checked_at: string;
  credential_version?: number | null;
  failure_reason?: string | null;
};

export type GatewayMessage = {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
};

export type GatewayRequestInput = {
  application_id: string;
  prompt?: string;
  messages?: GatewayMessage[];
  context?: Record<string, unknown>;
  source?: "playground" | "api" | "unknown";
  constraints?: Record<string, unknown>;
  strategies?: Record<string, string>;
  metadata?: Record<string, unknown>;
  understander?: string;
  decomposer?: string;
  router?: string;
  aggregator?: "auto" | "none" | "deterministic" | "llm";
};

export type GatewayRequestResponse = {
  request_id: string;
  application_id?: string;
  status: GatewayRequestStatus;
  output?: string | null;
  response?: unknown;
  provider?: string | null;
  model?: string | null;
  latency_ms?: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  estimated_cost?: number;
  fallback_used?: boolean;
  fallback_reason?: string | null;
  trace_count?: number;
};

export type GatewayRequestDetail = GatewayRequestResponse & {
  application_name?: string | null;
  application_deleted?: boolean;
  source: string;
  aggregation_status?: string | null;
  providers_used?: string[];
  models_used?: string[];
  prompt?: string | null;
  constraints?: Record<string, unknown>;
  response?: Record<string, unknown>;
  tasks: Array<Record<string, unknown>>;
  traces: Array<Record<string, unknown>>;
  executions: Array<Record<string, unknown>>;
  aggregation?: Record<string, unknown> | null;
  started_at?: string;
  finished_at?: string;
};

export type GatewayRequestSummary = {
  request_id: string;
  application_id: string;
  application_name?: string | null;
  application_deleted?: boolean;
  source: string;
  status: GatewayRequestStatus;
  aggregation_status?: string | null;
  total_cost: number;
  total_latency_ms: number;
  started_at: string;
  finished_at?: string | null;
};
export type GatewayRequestPage = PageResponse<GatewayRequestSummary>;

export type OverviewParams = {
  application_id?: string;
  scope?: "own" | "global";
  from?: string;
  to?: string;
};

export type OverviewResponse = {
  total_requests: number;
  success_rate: number;
  average_latency_ms: number;
  total_cost: number;
  active_model_count: number;
  recent_signals: Array<Record<string, unknown>>;
  from?: string;
  to?: string;
};

export type UsageParams = OverviewParams & {
  model?: string;
  model_id?: string;
  provider?: string;
  provider_id?: string;
  source?: "playground" | "api" | "unknown";
  status?: GatewayRequestStatus;
  page?: number;
  page_size?: number;
};

export type UsageRow = {
  request_id: string;
  timestamp: string;
  application_id: string;
  application: string;
  application_deleted: boolean;
  model?: string | null;
  provider?: string | null;
  status: GatewayRequestStatus;
  tokens: number;
  latency_ms: number;
  cost: number;
  fallback_used: boolean;
  source: "playground" | "api" | "unknown";
};

export type UsageResponse = PageResponse<UsageRow> & {
  summary: {
    requests: number;
    total_tokens: number;
    total_cost: number;
  };
  budget?: ApplicationBudgetSummary | null;
};

// Comparison contracts mirror src/shared/dtos/comparison.py and the report query.
export type ComparisonCreateInput = {
  application_id: string;
  gateway_request_id?: string | null;
  execution_strategy?: "sequential" | "parallel";
  prompt: string;
  context?: Record<string, unknown>;
  constraints?: Record<string, unknown>;
  reference_model_ids: string[];
  quality_evaluation?: boolean;
  evaluator_model_id?: string | null;
  rubric?: Record<string, unknown>;
  max_comparison_cost_usd?: number | null;
  idempotency_key: string;
};

export type ComparisonAccepted = {
  comparison_id: string;
  status: "queued" | "running" | "completed" | "partial" | "failed" | "indeterminate";
  replayed: boolean;
};

export type ComparisonBranch = {
  id: string;
  branch_key: string;
  branch_type: "gateway" | "reference";
  model_id?: string | null;
  model_name?: string | null;
  provider_name?: string | null;
  status: string;
  output?: string | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  usage_complete: boolean;
  pricing_estimated: boolean;
  latency_ms?: number | null;
  queue_delay_ms?: number | null;
  quality_score?: number | null;
  error_code?: string | null;
  error_message?: string | null;
};

export type ComparisonDelta = {
  reference_branch_id: string;
  reference_model_id?: string | null;
  cost_delta_usd?: number | null;
  cost_delta_percent?: number | null;
  latency_delta_ms?: number | null;
  latency_delta_percent?: number | null;
  quality_delta_points?: number | null;
  comparable_cost: boolean;
  comparable_latency: boolean;
  comparable_quality: boolean;
};

export type ComparisonRun = {
  id: string;
  application_id: string;
  gateway_request_id?: string | null;
  source: string;
  status: string;
  execution_mode: string;
  prompt: string;
  quality_evaluation: boolean;
  execution_strategy?: "sequential" | "parallel";
  evaluator_model_id?: string | null;
  evaluation_status?: string | null;
  evaluator_model_name?: string | null;
  evaluation_error_code?: string | null;
  evaluation_total_tokens: number;
  evaluation_error_message?: string | null;
  evaluation_cost_usd: number;
  evaluation_usage_complete: boolean;
  comparison_spend_usd: number;
  branches: ComparisonBranch[];
  deltas: ComparisonDelta[];
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error_code?: string | null;
  error_message?: string | null;
};

export type ComparisonCohort = {
  reference_model_id: string;
  samples: number;
  completed_pairs: number;
  comparable_cost_pairs: number;
  gateway_cost_usd: number;
  reference_cost_usd: number;
  cost_delta_usd: number | null;
  cost_delta_percent: number | null;
  gateway_latency_p50_ms: number | null;
  gateway_latency_p95_ms: number | null;
  reference_latency_p50_ms: number | null;
  reference_latency_p95_ms: number | null;
  gateway_quality_mean: number | null;
  reference_quality_mean: number | null;
};

export type ComparisonReport = {
  comparisons: number;
  evaluation_cost_usd: number;
  cohorts: ComparisonCohort[];
};

export type BillingTransaction = { id: string; type: string; amount: number; currency: string; status: string; timestamp: string; reference?: string | null; description: string; simulation?: boolean; };
export type BillingTransactionPage = PageResponse<BillingTransaction>;
export type BillingSettings = { low_balance_threshold: number; notify_low_balance: boolean; auto_topup_enabled: boolean; };
export type BillingSettingsInput = BillingSettings;
export type BillingTopupInput = { amount: number; currency?: string; idempotency_key: string; };
export type BillingTopupResponse = BillingTransaction & { simulation: true; };
export type ApplicationKey = { id: string; name: string; key_prefix: string; created_at: string; last_used_at?: string | null; revoked_at?: string | null; revoked: boolean; };
export type ApplicationKeyPage = PageResponse<ApplicationKey>;
export type ApplicationKeyCreateInput = { name: string };
export type ApplicationKeyCreated = ApplicationKey & { raw_key: string };

export type BillingAccount = {
  balance: number;
  currency: string;
  lifetime_topup: number;
  lifetime_usage: number;
  simulated_topup_used: boolean;
  last_transaction?: {
    type: string;
    amount: number;
    status: string;
  } | null;
};


export type IncidentSeverity = "low" | "medium" | "high" | "critical";
export type IncidentStatus = "investigating" | "monitoring" | "resolved";
export type Incident = { id: string; title: string; description: string; severity: IncidentSeverity; status: IncidentStatus; owner: string; type: string; scope: string; opened_at: string; duration_seconds: number; impact: string; application_id?: string | null; provider?: string | null; model?: string | null; updated_at: string; source?: "operational" | "user_report"; reporter_user_id?: string | null; reporter_email?: string | null; request_id?: string | null; steps_to_reproduce?: string; expected_behavior?: string; resolution_note?: string; };
export type IncidentParams = { application_id?: string; severity?: IncidentSeverity; status?: IncidentStatus; search?: string; page?: number; page_size?: number; };
export type IncidentCreateInput = { application_id?: string | null; title: string; description?: string; severity: IncidentSeverity; status?: IncidentStatus; owner?: string; type?: string; scope?: string; impact?: string; provider?: string; model?: string; };
export type IncidentUpdateInput = Partial<Pick<IncidentCreateInput, "title" | "description" | "severity" | "status" | "owner" | "type" | "scope" | "impact">> & { resolution_note?: string };
export type IncidentReportType = "application" | "model" | "logic" | "fallback" | "other";
export type IncidentReportInput = { title: string; description: string; type: IncidentReportType; application_id?: string; provider?: string; model?: string; request_id?: string; steps_to_reproduce?: string; expected_behavior?: string };
export type PolicyDecision = "approve" | "reject" | "request_changes";
export type PolicyReviewStatus = "pending" | "approved" | "rejected" | "changes_requested";
export type PolicyDecisionRecord = { id: string; actor_user_id: string; decision: PolicyDecision; comment: string; created_at: string; };
export type PolicyReview = { id: string; name: string; scope: string; type: string; submitted_by: string; submitted_at: string; status: PolicyReviewStatus; decision_history: PolicyDecisionRecord[]; affected_models: string[]; affected_applications: string[]; application_id?: string | null; };
export type PolicyReviewParams = { application_id?: string; status?: PolicyReviewStatus; page?: number; page_size?: number; };
export type PolicyDecisionInput = { decision: PolicyDecision; comment?: string; };
