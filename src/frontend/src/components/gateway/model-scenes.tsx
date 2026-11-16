"use client";

import Link from "next/link";
import { useCallback, useEffect, useReducer, useState } from "react";
import { createPortal } from "react-dom";
import { useLanguage } from "@/components/language-provider";

import {
  createModel,
  listAdminModels,
  listAvailableModels,
  listProviderConnections,
  probeModel,
  updateModel,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type {
  ModelCreateInput,
  ModelListParams,
  ModelListResponse,
  ModelProbeResponse,
  ModelFeatureDetail,
  ModelFeatureMetadata,
  ModelFeatureSupport,
  ModelUpdateInput,
  ModelSortField,
  ModelStatus,
  ModelSummary,
  ProviderConnection,
} from "@/lib/gateway-types";
import { PaginationControls, TABLE_PAGE_SIZE } from "@/components/gateway/pagination-controls";
import { FormPageSkeleton, ListPageSkeleton, ModelCatalogSkeleton } from "@/components/gateway/gateway-page-skeletons";
import { FilterBuilder, type FilterCondition } from "@/components/gateway/filter-builder";
import { SortPrioritySummary, SortableTableHeader, sortQueryParams, toggleSortCriterion, type SortCriterion } from "@/components/gateway/multi-sort";

function apiMessage(error: unknown, fallback: string): string {
  return error && typeof error === "object" && "message" in error
    ? String((error as GatewayApiError).message)
    : fallback;
}

function CatalogState({ message, error = false }: { message: string; error?: boolean }) {
  return (
    <div className={error ? "notice notice--warning" : "notice"} role="status">
      {message}
    </div>
  );
}

export function modelHealthTone(status: string | null | undefined): "healthy" | "watch" | "incident" | "neutral" {
  const normalized = status?.trim().toLowerCase() ?? "";
  if (["unhealthy", "failed", "offline", "down", "error"].includes(normalized)) return "incident";
  if (["degraded", "warning", "watch"].includes(normalized)) return "watch";
  if (["healthy", "available", "operational"].includes(normalized)) return "healthy";
  return "neutral";
}

export function modelHealthLabel(status: string | null | undefined, language: "vi" | "en" = "en"): string {
  const normalized = status?.trim();
  if (!normalized) return language === "vi" ? "Chưa rõ" : "Unknown";
  if (language === "vi") {
    const labels: Record<string, string> = { healthy: "Ổn định", available: "Khả dụng", operational: "Đang vận hành", unhealthy: "Không ổn định", failed: "Thất bại", offline: "Ngoại tuyến", down: "Ngừng hoạt động", error: "Lỗi", degraded: "Suy giảm", warning: "Cảnh báo", watch: "Cần theo dõi" };
    if (labels[normalized.toLowerCase()]) return labels[normalized.toLowerCase()];
  }
  return normalized.replace(/[_-]+/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function ModelHealthBadge({ status }: { status: string | null | undefined }) {
  const { t, language } = useLanguage();
  const label = modelHealthLabel(status, language);
  return <span className={`gateway-status model-health-badge gateway-status--${modelHealthTone(status)}`} title={t("models.healthLabel", { status: label })}><span className="status-pin" aria-hidden="true" />{label}</span>;
}

type ModelLatencySource = Pick<ModelSummary, "target_latency_ms" | "last_probe_latency_ms" | "feature_metadata">;

export function modelFeatureLatencyMetrics(model: ModelLatencySource) {
  const reference = model.feature_metadata?.latency_reference;
  return {
    targetLatencyMs: model.target_latency_ms ?? null,
    probeLatencyMs: model.last_probe_latency_ms ?? null,
    referenceLatencyMs: reference?.latency_ms ?? null,
    referenceDate: reference?.retrieved_on ?? null,
    referenceUrl: reference?.source_url ?? null,
  };
}

export function modelFeaturePricingMetrics(pricing: ModelFeatureMetadata["pricing"]) {
  const longContextScale = pricing.long_context_input_threshold != null
    && pricing.long_context_input_per_million != null
    && pricing.standard_input_per_million > 0
    ? pricing.long_context_input_per_million / pricing.standard_input_per_million
    : null;
  return {
    standardCache: {
      write5m: pricing.cache_write_5m_input_per_million,
      write1h: pricing.cache_write_1h_input_per_million,
      read: pricing.cache_read_input_per_million,
    },
    longContextCache: longContextScale == null ? null : {
      write5m: pricing.cache_write_5m_input_per_million * longContextScale,
      write1h: pricing.cache_write_1h_input_per_million * longContextScale,
      read: pricing.cache_read_input_per_million * longContextScale,
    },
  };
}

function formattedNumber(value: number, language: "vi" | "en"): string {
  return new Intl.NumberFormat(language === "vi" ? "vi-VN" : "en-US", { maximumFractionDigits: 6 }).format(value);
}

function featureKeyLabel(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function ModelFeatureRows({ features, language }: { features: Record<string, ModelFeatureDetail>; language: "vi" | "en" }) {
  const { t } = useLanguage();
  const supportLabels: Record<ModelFeatureSupport, string> = {
    supported: t("models.featureSupported"),
    conditional: t("models.featureConditional"),
    unverified: t("models.featureUnverified"),
    unsupported: t("models.featureUnsupported"),
  };
  return <ul className="model-feature-list">{Object.entries(features).map(([key, detail]) => <li key={key}>
    <div className="model-feature-row-heading"><strong>{featureKeyLabel(key)}</strong><span className={`model-feature-support model-feature-support--${detail.support}`}>{supportLabels[detail.support]}</span></div>
    {detail.conditions.length > 0 && <ul className="model-feature-conditions">{detail.conditions.map((condition) => <li key={condition}>{condition}</li>)}</ul>}
    <a href={detail.source_url} target="_blank" rel="noopener noreferrer">{language === "vi" ? "Nguồn tài liệu" : "Documentation source"}</a>
  </li>)}</ul>;
}

export function ModelFeatureOverview({ model }: { model: ModelSummary }) {
  const { t, language } = useLanguage();
  const metadata: ModelFeatureMetadata | null | undefined = model.feature_metadata;
  const latency = modelFeatureLatencyMetrics(model);
  const reference = metadata?.latency_reference;
  const verificationLabel = metadata?.provenance.verification === "live_verified"
    ? t("models.featureLiveVerified")
    : t("models.featureDocumentationOnly");
  const lifecycle = metadata?.lifecycle;
  const pricing = metadata?.pricing;
  const cachePricing = pricing ? modelFeaturePricingMetrics(pricing) : null;
  const formatLatency = (value: number | null) => value == null ? "—" : `${formattedNumber(value, language)} ms`;
  const formatUsd = (value: number) => `${formattedNumber(value, language)} USD / 1M`;

  return <section className="model-feature-overview" aria-label={t("models.featureOverviewTitle")}>
    <header className="model-feature-overview-header">
      <div><h3>{t("models.featureOverviewTitle")}</h3><p>{verificationLabel}{metadata?.provenance.checked_on ? ` · ${t("models.featureCheckedOn", { date: metadata.provenance.checked_on })}` : ""}</p></div>
      <span className={`gateway-status gateway-status--${model.status === "active" ? "healthy" : "neutral"}`}>{t(model.status === "active" ? "models.active" : "models.inactive")}</span>
    </header>

    <dl className="model-feature-facts">
      <div><dt>{t("models.contextWindow")}</dt><dd>{model.context_window == null ? "—" : formattedNumber(model.context_window, language)}</dd></div>
      <div><dt>{t("models.outputLimit")}</dt><dd>{model.max_output_tokens == null ? "—" : formattedNumber(model.max_output_tokens, language)}</dd></div>
      <div><dt>{t("models.catalogLatencyBaseline")}</dt><dd>{formatLatency(latency.targetLatencyMs)}</dd></div>
      <div><dt>{t("models.probeLatencyObserved")}</dt><dd>{formatLatency(latency.probeLatencyMs)}</dd></div>
    </dl>

    {pricing && <section className="model-feature-block">
      <h4>{t("models.featurePricing")}</h4>
      <div className="model-feature-price-grid">
        <div><strong>{t("models.featureStandardTier")}</strong><span>{t("models.inputShort")}: {formatUsd(pricing.standard_input_per_million)}</span><span>{t("models.outputShort")}: {formatUsd(pricing.standard_output_per_million)}</span></div>
        {pricing.long_context_input_threshold != null && pricing.long_context_input_per_million != null && pricing.long_context_output_per_million != null && <div><strong>{t("models.featureLongContextTier", { operator: pricing.long_context_threshold_operator ?? ">", tokens: formattedNumber(pricing.long_context_input_threshold, language) })}</strong><span>{t("models.inputShort")}: {formatUsd(pricing.long_context_input_per_million)}</span><span>{t("models.outputShort")}: {formatUsd(pricing.long_context_output_per_million)}</span></div>}
        <div><strong>{t("models.featureCacheRates")}</strong><span>{t("models.featureCacheWrite5m")}: {formatUsd(cachePricing!.standardCache.write5m)}</span><span>{t("models.featureCacheWrite1h")}: {formatUsd(cachePricing!.standardCache.write1h)}</span><span>{t("models.featureCacheRead")}: {formatUsd(cachePricing!.standardCache.read)}</span>{cachePricing?.longContextCache && <><strong>{t("models.featureLongContextCacheRates", { operator: pricing.long_context_threshold_operator ?? ">", tokens: formattedNumber(pricing.long_context_input_threshold!, language) })}</strong><span>{t("models.featureCacheWrite5m")}: {formatUsd(cachePricing.longContextCache.write5m)}</span><span>{t("models.featureCacheWrite1h")}: {formatUsd(cachePricing.longContextCache.write1h)}</span><span>{t("models.featureCacheRead")}: {formatUsd(cachePricing.longContextCache.read)}</span></>}</div>
      </div>
    </section>}

    {lifecycle && <section className="model-feature-block model-feature-lifecycle">
      <h4>{t("models.featureLifecycle")}</h4>
      <p><strong>{lifecycle.stage}</strong>{lifecycle.reliable_knowledge_cutoff ? ` · ${t("models.featureKnowledgeCutoff", { date: lifecycle.reliable_knowledge_cutoff })}` : ""}{lifecycle.retirement_commitment ? ` · ${t("models.featureRetirement", { date: lifecycle.retirement_commitment })}` : ""}</p>
    </section>}

    {reference && <section className="model-feature-block model-latency-reference">
      <h4>{t("models.externalLatencyReference")}</h4>
      <p><strong>{formatLatency(latency.referenceLatencyMs)} · P50 / TTFT</strong></p>
      <p>{t("models.externalLatencyContext", { throughput: formattedNumber(reference.throughput_tokens_per_second, language), window: reference.window })} · {t("models.featureCheckedOn", { date: reference.retrieved_on })}</p>
      <a href={reference.source_url} target="_blank" rel="noopener noreferrer">OpenRouter · {model.display_name}</a>
    </section>}

    {metadata ? <>
      <section className="model-feature-block"><h4>{t("models.upstreamFeatures")}</h4><details><summary>{t("models.featureDetailsCount", { count: Object.keys(metadata.upstream_features).length })}</summary><ModelFeatureRows features={metadata.upstream_features} language={language} /></details></section>
      <section className="model-feature-block"><h4>{t("models.gatewayFeatures")}</h4><details><summary>{t("models.featureDetailsCount", { count: Object.keys(metadata.gateway_features).length })}</summary><ModelFeatureRows features={metadata.gateway_features} language={language} /></details></section>
      <section className="model-feature-block"><h4>{t("models.featureConstraints")}</h4><dl className="model-feature-facts model-feature-facts--constraints">
        <div><dt>{t("models.featureThinking")}</dt><dd>{metadata.constraints.thinking_mode} · {metadata.constraints.default_effort}</dd></div>
        <div><dt>{t("models.featureForcedToolChoice")}</dt><dd>{metadata.constraints.forced_tool_choice}</dd></div>
        <div><dt>{t("models.featureCustomSampling")}</dt><dd>{metadata.constraints.custom_sampling ? t("usage.yes") : t("usage.no")}</dd></div>
        <div><dt>{t("models.featurePromptCaching")}</dt><dd>{metadata.constraints.prompt_caching ? t("usage.yes") : t("usage.no")}</dd></div>
        <div><dt>{t("models.featureTokenCounting")}</dt><dd>{metadata.constraints.token_counting ? t("usage.yes") : t("usage.no")}</dd></div>
      </dl></section>
    </> : <p className="model-feature-empty">{t("models.featureMetadataUnavailable")}</p>}
  </section>;
}

type CapabilityPresentation = { label: string; description: string; icon: string };

const CAPABILITY_PRESENTATIONS: Record<string, CapabilityPresentation> = {
  text: { label: "Text", description: "Understands and generates natural-language text.", icon: "Aa" },
  chat: { label: "Conversation", description: "Supports multi-turn messages and conversational context.", icon: "↔" },
  image: { label: "Vision", description: "Accepts images as input and interprets visual content.", icon: "◈" },
  vision: { label: "Vision", description: "Accepts images as input and interprets visual content.", icon: "◈" },
  file: { label: "File analysis", description: "Reads supported documents and uses their contents as context.", icon: "▤" },
  structured_output: { label: "Structured output", description: "Returns predictable, schema-friendly responses such as JSON.", icon: "{}" },
  reasoning: { label: "Reasoning", description: "Handles multi-step analysis and more complex problem solving.", icon: "◎" },
  tool_calling: { label: "Tool use", description: "Can select and call approved tools or application functions.", icon: "⌘" },
  tool_use: { label: "Tool use", description: "Can select and call approved tools or application functions.", icon: "⌘" },
  long_context: { label: "Long context", description: "Works with larger prompts and longer conversation histories.", icon: "↗" },
  embedding: { label: "Embeddings", description: "Converts content into vectors for search and similarity workflows.", icon: "▦" },
  audio: { label: "Audio", description: "Accepts or produces audio for supported speech workflows.", icon: "♪" },
  code: { label: "Code", description: "Understands, generates and transforms source code.", icon: "</>" },
  code_generation: { label: "Code generation", description: "Understands, generates and transforms source code.", icon: "</>" },
  classification: { label: "Classification", description: "Categorizes input text and intent into predefined classes.", icon: "⊞" },
  summarization: { label: "Summarization", description: "Condenses lengthy text and transcripts into concise summaries.", icon: "≡" },
  rerank: { label: "Rerank", description: "Reranks document chunks or candidate results by relevance.", icon: "↕" },
};

function normalizeCapability(capability: string): string {
  return capability.trim().toLowerCase().replace(/[\s-]+/g, "_");
}

export function capabilityPresentation(capability: string, language: "vi" | "en" = "en"): CapabilityPresentation {
  const normalized = normalizeCapability(capability);
  const presentation = CAPABILITY_PRESENTATIONS[normalized] ?? {
    label: normalized.replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase()) || "Additional capability",
    description: "An additional capability enabled for this model.",
    icon: "+",
  };
  if (language === "en") return presentation;
  const labels: Record<string, [string, string]> = {
    text: ["Văn bản", "Hiểu và tạo văn bản ngôn ngữ tự nhiên."], chat: ["Hội thoại", "Hỗ trợ tin nhắn nhiều lượt và ngữ cảnh hội thoại."],
    image: ["Thị giác", "Nhận hình ảnh đầu vào và diễn giải nội dung trực quan."], vision: ["Thị giác", "Nhận hình ảnh đầu vào và diễn giải nội dung trực quan."],
    file: ["Phân tích tệp", "Đọc tài liệu được hỗ trợ và dùng nội dung làm ngữ cảnh."], structured_output: ["Đầu ra có cấu trúc", "Trả về phản hồi ổn định, phù hợp schema như JSON."],
    reasoning: ["Suy luận", "Xử lý phân tích nhiều bước và bài toán phức tạp."], tool_calling: ["Sử dụng công cụ", "Có thể chọn và gọi công cụ hoặc hàm ứng dụng được cho phép."], tool_use: ["Sử dụng công cụ", "Có thể chọn và gọi công cụ hoặc hàm ứng dụng được cho phép."],
    long_context: ["Ngữ cảnh dài", "Làm việc với prompt lớn và lịch sử hội thoại dài."], embedding: ["Embedding", "Chuyển nội dung thành vector cho tìm kiếm và so khớp tương đồng."],
    audio: ["Âm thanh", "Nhận hoặc tạo âm thanh cho quy trình giọng nói được hỗ trợ."], code: ["Mã nguồn", "Hiểu, tạo và biến đổi mã nguồn."], code_generation: ["Sinh mã", "Hiểu, tạo và biến đổi mã nguồn."],
    classification: ["Phân loại", "Phân loại văn bản và ý định đầu vào theo nhóm định trước."], summarization: ["Tóm tắt", "Rút gọn văn bản dài và bản ghi thành bản tóm tắt súc tích."], rerank: ["Xếp hạng lại", "Xếp hạng lại đoạn tài liệu hoặc kết quả theo độ liên quan."],
  };
  const localized = labels[normalized];
  return localized ? { ...presentation, label: localized[0], description: localized[1] } : { ...presentation, description: "Khả năng bổ sung được bật cho model này." };
}

export function modelDifficultyPresentation(qualityTier: string | null | undefined, language: "vi" | "en" = "en"): { label: string; tone: "simple" | "mid" | "hard" | "unknown" } {
  const normalized = qualityTier?.trim().toLowerCase();
  if (normalized === "simple") return { label: language === "vi" ? "Đơn giản" : "Simple", tone: "simple" };
  if (normalized === "mid" || normalized === "medium") return { label: language === "vi" ? "Trung bình" : "Mid", tone: "mid" };
  if (normalized === "hard" || normalized === "complex") return { label: language === "vi" ? "Khó" : "Hard", tone: "hard" };
  return { label: language === "vi" ? "Chưa xếp hạng" : "Unrated", tone: "unknown" };
}

function ModelDifficultyBadge({ qualityTier }: { qualityTier: string | null | undefined }) {
  const { t, language } = useLanguage();
  const difficulty = modelDifficultyPresentation(qualityTier, language);
  return <span className={`model-difficulty model-difficulty--${difficulty.tone}`} title={t("models.routingDifficulty", { difficulty: difficulty.label })}>
    <i className="model-difficulty-icon" aria-hidden="true"><b /><b /><b /></i>
    {difficulty.label}
  </span>;
}

function CapabilityList({ model }: { model: ModelSummary }) {
  const { t, language } = useLanguage();
  const [open, setOpen] = useState(false);
  const uniqueCapabilities = [...new Set(model.capabilities.map(normalizeCapability).filter(Boolean))];
  const presented = uniqueCapabilities.map((capability) => ({ raw: capability, ...capabilityPresentation(capability, language) }));

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [open]);

  if (!presented.length) return <span className="model-capability-empty">{t("models.notSpecified")}</span>;
  const preview = presented.slice(0, 2);
  const hiddenCount = presented.length - preview.length;

  return <>
    <div className="model-capability-list" aria-label={t("models.capabilityCount", { count: presented.length, model: model.display_name })}>
      {preview.map((capability) => <span key={capability.raw}>{capability.label}</span>)}
      <button className="model-capability-details-trigger" type="button" onClick={() => setOpen(true)} aria-haspopup="dialog">
        {hiddenCount > 0 ? `+${hiddenCount}` : t("models.details")}<i aria-hidden="true">→</i>
      </button>
    </div>
    {open && typeof document !== "undefined" && createPortal(
      <div className="model-capability-dialog-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) setOpen(false); }}>
        <section className="model-capability-dialog" role="dialog" aria-modal="true" aria-labelledby="model-capability-dialog-title">
          <header>
            <div><span>{t("models.capabilities")}</span><h2 id="model-capability-dialog-title">{t("models.capabilityTitle", { model: model.display_name })}</h2><p>{t("models.capabilityDescription")}</p></div>
            <button type="button" onClick={() => setOpen(false)} aria-label={t("models.closeCapabilityDetails")} autoFocus>×</button>
          </header>
          <ul>{presented.map((capability) => <li key={capability.raw}>
            <span className="model-capability-icon" aria-hidden="true">{capability.icon}</span>
            <div><strong>{capability.label}</strong><p>{capability.description}</p></div>
          </li>)}</ul>
          <ModelFeatureOverview model={model} />
        </section>
      </div>,
      document.querySelector(".gateway-shell") ?? document.body,
    )}
  </>;
}

function ModelFilters({ onProvider, onSearch, onStatus }: { onProvider: (value: string) => void; onSearch: (value: string) => void; onStatus: (value: string) => void }) {
  const { t } = useLanguage();
  const [filters, setFilters] = useState<FilterCondition[]>([]);
  function updateFilters(next: FilterCondition[]) {
    setFilters(next);
    onSearch(next.find((item) => item.field === "name" && item.value.trim())?.value.trim() ?? "");
    onProvider(next.find((item) => item.field === "provider" && item.value.trim())?.value.trim() ?? "");
    onStatus(next.find((item) => item.field === "status" && item.value)?.value ?? "");
  }
  return <FilterBuilder fields={[{ key: "name", label: t("models.name"), type: "text" }, { key: "provider", label: t("models.provider"), type: "text" }, { key: "status", label: t("models.status"), type: "enum", options: [{ value: "active", label: t("models.active") }, { value: "inactive", label: t("models.inactive") }] }]} value={filters} onChange={updateFilters} />;
}

function useCatalog(
  loader: (params: ModelListParams) => Promise<ModelListResponse>,
  provider: string,
  search: string,
  status: string,
  page: number,
  revision = 0,
) {
  const { t } = useLanguage();
  const [response, setResponse] = useState<ModelListResponse | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void loader({
      provider: provider || undefined,
      search: search || undefined,
      status: (status || undefined) as ModelStatus | undefined,
      page,
      page_size: TABLE_PAGE_SIZE,
    }).then((value) => {
      if (!active) return;
      setResponse(value);
      setState("ready");
      setError(null);
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("error");
      setError(apiMessage(reason, t("models.error")));
    });
    return () => { active = false; };
  }, [loader, page, provider, revision, search, status, t]);

  return { response, state, error };
}

export type ModelAdminState = { selectedModel: ModelSummary | null; catalogRevision: number };
export type ModelAdminAction =
  | { type: "edit"; model: ModelSummary }
  | { type: "close" }
  | { type: "refresh" };

export function modelAdminReducer(state: ModelAdminState, action: ModelAdminAction): ModelAdminState {
  if (action.type === "edit") return { ...state, selectedModel: action.model };
  if (action.type === "close") return { ...state, selectedModel: null };
  return { ...state, catalogRevision: state.catalogRevision + 1 };
}

function ModelCard({ model }: { model: ModelListResponse["items"][number] }) {
  const { t } = useLanguage();
  return (
    <article className="model-card">
      <div><span className="provider-code">{model.provider.slice(0, 2).toUpperCase()}</span><ModelHealthBadge status={model.health_status} /></div>
      <h2>{model.display_name}</h2>
      <p>{model.provider} · {model.name}</p>
      <ModelDifficultyBadge qualityTier={model.quality_tier} />
      <CapabilityList model={model} />
      <dl><div><dt>{t("models.context")}</dt><dd>{model.context_window ?? "—"}</dd></div><div><dt>{t("models.targetLatency")}</dt><dd>{model.target_latency_ms == null ? "—" : `${model.target_latency_ms} ms`}</dd></div><div><dt>{t("models.usedBy")}</dt><dd>{model.used_by_count}</dd></div></dl>
    </article>
  );
}

function ModelIdentity({ model }: { model: ModelListResponse["items"][number] }) {
  return <div className="model-identity">
    <strong>{model.display_name}</strong>
    <div className="model-identity-meta"><small>{model.name}</small><span aria-hidden="true">·</span><ModelDifficultyBadge qualityTier={model.quality_tier} /></div>
  </div>;
}

export function AvailableModelsScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const [provider, setProvider] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("active");
  const [page, setPage] = useState(1);
  const loader = useCallback((params: ModelListParams) => listAvailableModels(params), []);
  const catalog = useCatalog(loader, provider, search, status, page);
  const reset = (setter: (value: string) => void) => (value: string) => { setter(value); setPage(1); };
  if (!permissions.includes("models.read_available")) return <CatalogState message={t("models.noPermission")} error />;
  if (catalog.state === "loading") return <ModelCatalogSkeleton label={t("models.loadingAvailable")} />;
  return (
    <>
      <section className="gateway-page-heading"><div><p className="gateway-eyebrow">{t("models.catalogue")}</p><h1>{t("models.availableTitle")}</h1><p>{t("models.availableDescription")}</p></div></section>
      <ModelFilters onProvider={reset(setProvider)} onSearch={reset(setSearch)} onStatus={reset(setStatus)} />
      {catalog.state === "error" && <CatalogState message={catalog.error ?? t("models.errorAvailable")} error />}
      {catalog.state === "ready" && catalog.response?.items.length === 0 && <CatalogState message={t("models.noAvailable")} />}
      {catalog.state === "ready" && catalog.response && catalog.response.items.length > 0 && <><section className="model-grid">{catalog.response.items.map((model) => <ModelCard key={model.id} model={model} />)}</section><PaginationControls page={catalog.response.page} pageSize={catalog.response.page_size} total={catalog.response.total} onPageChange={setPage} label={t("models.availablePagination")} /></>}
    </>
  );
}

export function UnifiedModelsScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const [provider, setProvider] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [sortCriteria, setSortCriteria] = useState<SortCriterion<ModelSortField>[]>([]);
  const [modelAdminState, dispatchModelAdmin] = useReducer(modelAdminReducer, { selectedModel: null, catalogRevision: 0 });
  const canReadAdmin = permissions.includes("models.read");
  const canReadAvailable = permissions.includes("models.read_available");
  const canRead = canReadAdmin || canReadAvailable;
  const [status, setStatus] = useState(canReadAdmin ? "" : "active");
  const loader = useCallback((params: ModelListParams) => canReadAdmin ? listAdminModels(params) : listAvailableModels(params), [canReadAdmin]);
  const sortedLoader = useCallback((params: ModelListParams) => loader({
    ...params,
    ...sortQueryParams(sortCriteria),
  }), [loader, sortCriteria]);
  const catalog = useCatalog(sortedLoader, provider, search, status, page, modelAdminState.catalogRevision);
  const reset = (setter: (value: string) => void) => (value: string) => { setter(value); setPage(1); };
  function updateSort(next: ModelSortField) {
    setSortCriteria((current) => toggleSortCriterion(current, next));
    setPage(1);
  }

  const sortLabels: Record<ModelSortField, string> = { name: t("models.model"), provider: t("models.provider"), status: t("models.status"), quality_tier: t("models.quality"), context: t("models.context"), latency: t("models.health"), input_price: t("models.inputPrice"), output_price: t("models.outputPrice"), used_by: t("models.usedBy") };

  if (!canRead) return <CatalogState message={t("models.noPermission")} error />;
  if (catalog.state === "loading") return <ListPageSkeleton label={t("models.loading")} filters={3} columns={9} />;
  return (
    <>
      <section className="gateway-page-heading">
        <div><p className="gateway-eyebrow">{t("models.catalogue")}</p><h1>{t("models.title")}</h1><p>{canReadAdmin ? t("models.descriptionAdmin") : t("models.descriptionAvailable")}</p></div>
        {permissions.includes("models.manage") && <Link href="/models/new" className="gateway-button">{t("models.add")}</Link>}
      </section>
      <ModelFilters onProvider={reset(setProvider)} onSearch={reset(setSearch)} onStatus={reset(setStatus)} />
      {catalog.state === "error" && <CatalogState message={catalog.error ?? t("models.error")} error />}
      {catalog.state === "ready" && catalog.response?.items.length === 0 && <CatalogState message={t("models.noMatch")} />}
      {catalog.state === "ready" && catalog.response && catalog.response.items.length > 0 && <section className="gateway-panel table-panel">
        <SortPrioritySummary criteria={sortCriteria} labels={sortLabels} />
        <div className="gateway-table-wrap"><table className="gateway-table model-catalog-table"><thead><tr>
          <SortableTableHeader field="name" label={t("models.model")} criteria={sortCriteria} onToggle={updateSort} />
          <SortableTableHeader field="provider" label={t("models.provider")} criteria={sortCriteria} onToggle={updateSort} />
          <th>{t("models.status")}</th><th>{t("models.capabilities")}</th>
          <SortableTableHeader field="context" label={t("models.context")} criteria={sortCriteria} onToggle={updateSort} />
          <SortableTableHeader field="input_price" label={t("models.inputPrice")} criteria={sortCriteria} onToggle={updateSort} />
          <SortableTableHeader field="output_price" label={t("models.outputPrice")} criteria={sortCriteria} onToggle={updateSort} />
          <th>{t("models.health")}</th>
          <SortableTableHeader field="used_by" label={t("models.usedBy")} criteria={sortCriteria} onToggle={updateSort} />
          {permissions.includes("models.manage") && <th className="model-catalog-actions-column">{t("applications.action")}</th>}
        </tr></thead><tbody>
          {catalog.response.items.map((model) => <tr key={model.id}>
            <td className="table-identity-cell"><ModelIdentity model={model} /></td>
            <td className="table-cell-nowrap">{model.provider}</td>
            <td className="table-cell-nowrap"><span className={`gateway-status gateway-status--${model.status === "active" ? "healthy" : "neutral"}`}>{model.status}</span></td>
            <td><CapabilityList model={model} /></td>
            <td className="table-cell-nowrap numeric">{model.context_window ?? "—"}</td>
            <td className="table-cell-nowrap numeric">{model.pricing.input_per_million ?? "—"}</td>
            <td className="table-cell-nowrap numeric">{model.pricing.output_per_million ?? "—"}</td>
            <td className="table-cell-nowrap"><ModelHealthBadge status={model.health_status} /></td>
            <td className="table-cell-nowrap numeric">{model.used_by_count ?? 0}</td>
            {permissions.includes("models.manage") && <td className="gateway-table-actions-cell model-catalog-actions-column"><button type="button" className="gateway-button gateway-button--ghost model-catalog-action" onClick={() => dispatchModelAdmin({ type: "edit", model })}>{t("models.editProbe")}</button></td>}
          </tr>)}
        </tbody></table></div>
        <PaginationControls page={catalog.response.page} pageSize={catalog.response.page_size} total={catalog.response.total} onPageChange={setPage} label={t("models.pagination")} />
      </section>}
      {modelAdminState.selectedModel && <ModelMetadataEditor model={modelAdminState.selectedModel} onClose={() => dispatchModelAdmin({ type: "close" })} onSaved={() => dispatchModelAdmin({ type: "refresh" })} />}
    </>
  );
}

export function ModelsAdminScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const [provider, setProvider] = useState("");
  const [search, setSearch] = useState("");
  const canReadAdmin = permissions.includes("models.read");
  const canReadAvailable = permissions.includes("models.read_available");
  const canRead = canReadAdmin || canReadAvailable;
  const [status, setStatus] = useState(canReadAdmin ? "" : "active");
  const [page, setPage] = useState(1);
  const loader = useCallback((params: ModelListParams) => canReadAdmin ? listAdminModels(params) : listAvailableModels(params), [canReadAdmin]);
  const catalog = useCatalog(loader, provider, search, status, page);
  const reset = (setter: (value: string) => void) => (value: string) => { setter(value); setPage(1); };
  if (!canRead) return <CatalogState message={t("models.noPermission")} error />;
  if (catalog.state === "loading") return <ListPageSkeleton label={t("models.loading")} filters={3} columns={7} />;
  return (
    <>
      <section className="gateway-page-heading"><div><p className="gateway-eyebrow">{t("models.catalogue")}</p><h1>{t("models.title")}</h1><p>{canReadAdmin ? t("models.descriptionAdmin") : t("models.descriptionAvailable")}</p></div>{permissions.includes("models.manage") && <Link href="/models/new" className="gateway-button">{t("models.add")}</Link>}</section>
      <ModelFilters onProvider={reset(setProvider)} onSearch={reset(setSearch)} onStatus={reset(setStatus)} />
      {catalog.state === "error" && <CatalogState message={catalog.error ?? t("models.error")} error />}
      {catalog.state === "ready" && catalog.response?.items.length === 0 && <CatalogState message={t("models.noMatch")} />}
      {catalog.state === "ready" && catalog.response && catalog.response.items.length > 0 && <section className="gateway-panel table-panel"><div className="gateway-table-wrap"><table className="gateway-table model-catalog-table"><thead><tr><th>{t("models.model")}</th><th>{t("models.provider")}</th><th>{t("models.capabilities")}</th><th>{t("models.context")}</th><th>{t("models.pricing")}</th><th>{t("models.health")}</th><th>{t("models.usedBy")}</th></tr></thead><tbody>{catalog.response.items.map((model) => <tr key={model.id}><td className="table-identity-cell"><ModelIdentity model={model} /></td><td className="table-cell-nowrap">{model.provider}</td><td><CapabilityList model={model} /></td><td className="table-cell-nowrap numeric">{model.context_window ?? "—"}</td><td className="table-cell-nowrap numeric">{model.pricing.input_per_million} {t("models.inputShort")} · {model.pricing.output_per_million} {t("models.outputShort")}</td><td className="table-cell-nowrap"><ModelHealthBadge status={model.health_status} /></td><td className="table-cell-nowrap numeric">{model.used_by_count}</td></tr>)}</tbody></table></div><PaginationControls page={catalog.response.page} pageSize={catalog.response.page_size} total={catalog.response.total} onPageChange={setPage} label={t("models.pagination")} /></section>}
    </>
  );
}

const knownCapabilities = ["text", "chat", "embedding", "vision", "audio", "reasoning", "structured_output", "code_generation", "tool_use", "classification", "summarization", "rerank", "long_context"];

export function normalizeCapabilities(values: readonly string[]): string[] {
  const found = new Set<string>();
  return values.map((value) => value.trim()).filter((value) => {
    const key = value.toLocaleLowerCase();
    if (!key || found.has(key)) return false;
    found.add(key);
    return true;
  });
}

export function splitCapabilities(values: readonly string[]): { selected: string[]; custom: string[] } {
  const normalized = normalizeCapabilities(values);
  const known = new Set(knownCapabilities);
  return {
    selected: normalized.filter((value) => known.has(value.toLocaleLowerCase())),
    custom: normalized.filter((value) => !known.has(value.toLocaleLowerCase())),
  };
}

export function canActivateProbedModel(input: {
  probeStatus?: string | null;
  probeCredentialVersion?: number | null;
  providerKind?: string;
  credentialConfigured?: boolean;
  credentialStatus?: string | null;
  currentCredentialVersion?: number | null;
}): boolean {
  if (input.probeStatus === "offline") return input.providerKind === "offline" || input.providerKind === "local";
  return input.probeStatus === "healthy"
    && input.credentialConfigured === true
    && input.credentialStatus === "active"
    && input.probeCredentialVersion != null
    && input.probeCredentialVersion === input.currentCredentialVersion;
}

export type ModelMetadataValues = {
  displayName: string;
  capabilities: readonly string[];
  qualityTier: string;
  contextWindow: string;
  targetLatency: string;
  inputPrice: string;
  outputPrice: string;
  pricingVersion: string;
};

export function buildModelCreateInput(providerId: string, name: string, values: ModelMetadataValues): ModelCreateInput {
  return {
    provider_id: providerId,
    name: name.trim(),
    display_name: values.displayName.trim(),
    status: "inactive",
    capabilities: normalizeCapabilities(values.capabilities),
    quality_tier: values.qualityTier,
    context_window: values.contextWindow === "" ? null : Number(values.contextWindow),
    target_latency_ms: values.targetLatency === "" ? null : Number(values.targetLatency),
    input_price_per_million: values.inputPrice === "" ? 0 : Number(values.inputPrice),
    output_price_per_million: values.outputPrice === "" ? 0 : Number(values.outputPrice),
    pricing_version: values.pricingVersion.trim() || "1",
  };
}

export function buildModelUpdate(current: ModelSummary, values: ModelMetadataValues): ModelUpdateInput {
  const update: ModelUpdateInput = {};
  const normalizedCapabilities = normalizeCapabilities(values.capabilities);
  const sameCapabilities = [...normalizedCapabilities].sort().join("\u0000") === [...current.capabilities].sort().join("\u0000");
  if (values.displayName.trim() !== current.display_name) update.display_name = values.displayName.trim();
  if (!sameCapabilities) update.capabilities = normalizedCapabilities;
  if (values.qualityTier !== current.quality_tier) update.quality_tier = values.qualityTier;
  if (values.contextWindow !== "" && Number(values.contextWindow) !== current.context_window) update.context_window = Number(values.contextWindow);
  if (values.targetLatency !== "" && Number(values.targetLatency) !== current.target_latency_ms) update.target_latency_ms = Number(values.targetLatency);
  if (values.inputPrice !== "" && Number(values.inputPrice) !== current.pricing.input_per_million) update.input_price_per_million = Number(values.inputPrice);
  if (values.outputPrice !== "" && Number(values.outputPrice) !== current.pricing.output_per_million) update.output_price_per_million = Number(values.outputPrice);
  if (values.pricingVersion.trim() !== current.pricing.version) update.pricing_version = values.pricingVersion.trim();
  return update;
}

type ModelEditorProps = {
  model?: ModelSummary;
  onClose?: () => void;
  onSaved?: () => void;
};

function ModelEditorDialogHeader({ title, onClose }: { title: string; onClose: () => void }) {
  const { t } = useLanguage();
  return <header className="model-editor-dialog-header">
    <div><p className="gateway-eyebrow">{t("models.catalogue")}</p><h2>{title}</h2><p>{t("models.probeDescription")}</p></div>
    <button className="model-editor-dialog-close" type="button" aria-label={t("models.closeEditor")} onClick={onClose}>×</button>
  </header>;
}

function ModelMetadataEditor({ model: initialModel, onClose, onSaved }: ModelEditorProps) {
  const { t, language } = useLanguage();
  const [providers, setProviders] = useState<ProviderConnection[]>([]);
  const [providerId, setProviderId] = useState(initialModel?.provider_id ?? "");
  const [name, setName] = useState(initialModel?.name ?? "");
  const [displayName, setDisplayName] = useState(initialModel?.display_name ?? "");
  const [qualityTier, setQualityTier] = useState(initialModel?.quality_tier ?? "mid");
  const [capabilities, setCapabilities] = useState<string[]>(() => splitCapabilities(initialModel?.capabilities ?? []).selected);
  const [customCapabilities, setCustomCapabilities] = useState(() => splitCapabilities(initialModel?.capabilities ?? []).custom.join(", "));
  const [contextWindow, setContextWindow] = useState(initialModel?.context_window?.toString() ?? "");
  const [targetLatency, setTargetLatency] = useState(initialModel?.target_latency_ms?.toString() ?? "");
  const [inputPrice, setInputPrice] = useState(initialModel?.pricing.input_per_million?.toString() ?? "0");
  const [outputPrice, setOutputPrice] = useState(initialModel?.pricing.output_per_million?.toString() ?? "0");
  const [pricingVersion, setPricingVersion] = useState(initialModel?.pricing.version ?? "1");
  const [savedModel, setSavedModel] = useState<ModelSummary | null>(initialModel ?? null);
  const [probe, setProbe] = useState<ModelProbeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void listProviderConnections().then((items) => {
      if (!active) return;
      const targets = items.filter((item) => item.purpose === "target" && (item.status === "active" || item.id === initialModel?.provider_id));
      setProviders(targets);
      setProviderId((current) => current || initialModel?.provider_id || targets[0]?.id || "");
      setLoading(false);
    }).catch((reason: unknown) => { if (active) { setError(apiMessage(reason, t("models.error"))); setLoading(false); } });
    return () => { active = false; };
  }, [initialModel?.provider_id, t]);

  const selectedProvider = providers.find((item) => item.id === providerId);
  const allCapabilities = normalizeCapabilities([...capabilities, ...customCapabilities.split(",")]);
  const probeStatus = probe?.status ?? savedModel?.health_status;
  const probeVersion = probe?.credential_version ?? savedModel?.last_probe_credential_version;
  const currentCredential = selectedProvider?.credential;
  const credentialSummary = selectedProvider?.credential.status === "revoked"
    ? `${t("models.credentialRevoked")}${selectedProvider.credential.key_last4 ? ` · ••••${selectedProvider.credential.key_last4}` : ""} · v${selectedProvider.credential.version}`
    : currentCredential?.configured
      ? `${t("models.credentialConfigured")} · v${currentCredential.version}`
      : selectedProvider?.kind === "offline" || selectedProvider?.kind === "local"
        ? t("models.credentialNotRequired")
        : t("models.credentialNotConfigured");
  const canActivate = Boolean(savedModel && canActivateProbedModel({
    probeStatus,
    probeCredentialVersion: probeVersion,
    providerKind: selectedProvider?.kind,
    credentialConfigured: currentCredential?.configured,
    credentialStatus: currentCredential?.status,
    currentCredentialVersion: currentCredential?.version,
  }));

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setWorking(true); setError(null); setNotice(null);
    const normalized = normalizeCapabilities(allCapabilities);
    try {
      let updated: ModelSummary;
      if (savedModel) {
        const update = buildModelUpdate(savedModel, { displayName, capabilities: normalized, qualityTier, contextWindow, targetLatency, inputPrice, outputPrice, pricingVersion });
        if (Object.keys(update).length === 0) {
          setNotice(t("models.noMetadataChanged")); setWorking(false); return;
        }
        updated = await updateModel(savedModel.id, update);
        const probeReset = "capabilities" in update || "context_window" in update;
        if (probeReset) setProbe(null);
        setNotice(probeReset ? t("models.metadataSavedProbe") : t("models.metadataSavedValid"));
      } else {
        const payload = buildModelCreateInput(providerId, name, { displayName, capabilities: normalized, qualityTier, contextWindow, targetLatency, inputPrice, outputPrice, pricingVersion });
        updated = await createModel(payload);
        setNotice(t("models.savedInactive"));
      }
      setSavedModel(updated); setName(updated.name); setProviderId(updated.provider_id ?? providerId); onSaved?.();
    } catch (reason) { setError(apiMessage(reason, t("models.error"))); }
    finally { setWorking(false); }
  }

  async function runProbe() {
    if (!savedModel) { setError(t("models.saveBeforeProbe")); return; }
    setWorking(true); setError(null); setNotice(null);
    try {
      const result = await probeModel(savedModel.id);
      setProbe(result);
      setSavedModel((current) => current ? { ...current, health_status: result.status, last_probe_at: result.checked_at, last_probe_credential_version: result.credential_version ?? null } : current);
      setNotice(t("models.probeResult", { status: modelHealthLabel(result.status, language), latency: Math.round(result.latency_ms), credential: result.credential_version == null ? t("models.noCredentialVersion") : `v${result.credential_version}`, reason: result.failure_reason ? ` · ${result.failure_reason}` : "" }));
      onSaved?.();
    } catch (reason) { setError(apiMessage(reason, t("models.error"))); }
    finally { setWorking(false); }
  }

  async function setActive(status: ModelStatus) {
    if (!savedModel) return;
    setWorking(true); setError(null); setNotice(null);
    try {
      const updated = await updateModel(savedModel.id, { status });
      setSavedModel(updated); setNotice(status === "active" ? t("models.activated") : t("models.deactivated")); onSaved?.();
    } catch (reason) { setError(apiMessage(reason, t("models.error"))); }
    finally { setWorking(false); }
  }

  if (loading) {
    const skeleton = <FormPageSkeleton label={t("models.loadingConnections")} fields={8} />;
    return onClose
      ? <div className="gateway-modal-backdrop model-editor-backdrop"><section className="model-editor-dialog" role="dialog" aria-modal="true" aria-label={t("models.editModel")}><ModelEditorDialogHeader title={t("models.editModel")} onClose={onClose} /><div className="model-editor-loading">{skeleton}</div></section></div>
      : skeleton;
  }
  const editor = <section className="gateway-panel form-panel model-editor-panel" aria-label={savedModel ? t("models.editMetadata") : t("models.addMetadata")}>
    {!onClose && (
      <header className="model-editor-card-header">
        <div className="model-editor-card-header-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m12 3 8 4.5-8 4.5-8-4.5L12 3Z" />
            <path d="m4 12 8 4.5 8-4.5" />
            <path d="m4 16.5 8 4.5 8-4.5" />
          </svg>
        </div>
        <div className="model-editor-card-header-copy">
          <p className="gateway-eyebrow">{t("models.platformModel")}</p>
          <h2>{savedModel ? t("models.editMetadata") : t("models.addUpstream")}</h2>
          <p>{t("models.editorDescription")}</p>
        </div>
        <span className="model-editor-required-badge">
          <i aria-hidden="true">*</i> {t("applications.required")}
        </span>
      </header>
    )}
    {error && <CatalogState message={error} error />}{notice && <CatalogState message={notice} error={Boolean(probe && probe.status !== "healthy" && probe.status !== "offline")} />}
    {providers.length === 0 && <CatalogState message={t("models.noTargetConnection")} error />}
    <form className="model-editor-form" onSubmit={submit}>
      <div className="model-editor-section">
        <div className="model-editor-section-header">
          <span className="model-editor-section-number">1</span>
          <div>
            <h3>{t("models.targetConnection")}</h3>
            <p>{t("models.targetConnectionDescription")}</p>
          </div>
        </div>
        <div className="model-editor-section-body">
          <label className="field full">
            <span>{t("models.targetConnectionRequired")}</span>
            <select required value={providerId} disabled={Boolean(savedModel)} onChange={(event) => setProviderId(event.target.value)}>
              <option value="" disabled>{t("models.selectTargetConnection")}</option>
              {providers.map((item) => <option key={item.id} value={item.id} disabled={item.status !== "active"}>{item.display_name} · {item.kind}{item.status !== "active" ? ` · ${t("models.inactive")}` : ""}</option>)}
            </select>
          </label>
          <div className="model-provider-status-strip">
            {selectedProvider ? (
              <>
                <span className={`provider-status-indicator provider-status-indicator--${selectedProvider.status === "active" ? "active" : "inactive"}`} aria-hidden="true" />
                <span className="provider-status-summary">{credentialSummary}</span>
                <Link href="/providers" className="provider-manage-link">
                  {t("models.manageConnections")}
                  <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 3l5 5-5 5" /></svg>
                </Link>
              </>
            ) : (
              <span className="provider-status-empty">
                {t("models.selectConnectionHint")} <Link href="/providers">{t("models.manageConnections")} →</Link>
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="model-editor-section">
        <div className="model-editor-section-header">
          <span className="model-editor-section-number">2</span>
          <div>
            <h3>{t("models.identification")}</h3>
            <p>{t("models.identificationDescription")}</p>
          </div>
        </div>
        <div className="model-editor-section-body form-grid-3">
          <label className="field">
            <span>{t("models.upstreamId")}</span>
            <input required value={name} disabled={Boolean(savedModel)} onChange={(event) => setName(event.target.value)} placeholder={t("models.upstreamPlaceholder")} />
            <small>{t("models.upstreamHint")}</small>
          </label>
          <label className="field">
            <span>{t("models.displayNameRequired")}</span>
            <input required value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder={t("models.displayNamePlaceholder")} />
            <small>{t("models.displayNameHint")}</small>
          </label>
          <label className="field">
            <span>{t("models.qualityTier")}</span>
            <select value={qualityTier} onChange={(event) => setQualityTier(event.target.value)}>
              <option value="simple">{t("models.tierSimple")}</option>
              <option value="mid">{t("models.tierMid")}</option>
              <option value="hard">{t("models.tierHard")}</option>
              {!(["simple", "mid", "hard"].includes(qualityTier)) && <option value={qualityTier}>{qualityTier}</option>}
            </select>
            <small>{t("models.qualityTierHint")}</small>
          </label>
        </div>
      </div>

      <div className="model-editor-section">
        <div className="model-editor-section-header">
          <span className="model-editor-section-number">3</span>
          <div>
            <h3>{t("models.capabilitiesModalities")}</h3>
            <p>{t("models.capabilitiesModalitiesDescription")}</p>
          </div>
        </div>
        <div className="model-editor-section-body">
          <div className="model-capability-chips-grid">
            {knownCapabilities.map((capability) => {
              const isChecked = capabilities.some((item) => item.toLocaleLowerCase() === capability);
              const presentation = capabilityPresentation(capability, language);
              return (
                <label key={capability} className={`model-capability-chip ${isChecked ? "is-selected" : ""}`}>
                  <input
                    type="checkbox"
                    checked={isChecked}
                    onChange={(event) => setCapabilities((current) => event.target.checked ? normalizeCapabilities([...current, capability]) : current.filter((item) => item.toLocaleLowerCase() !== capability))}
                  />
                  <span className="model-capability-chip-icon" aria-hidden="true">{presentation.icon}</span>
                  <span className="model-capability-chip-label">{presentation.label}</span>
                  {isChecked && <span className="model-capability-chip-check" aria-hidden="true">✓</span>}
                </label>
              );
            })}
          </div>
          <div className="model-custom-labels-wrap">
            <label className="field full">
              <span>{t("models.additionalLabels")}</span>
              <input value={customCapabilities} onChange={(event) => setCustomCapabilities(event.target.value)} placeholder={t("models.additionalLabelsPlaceholder")} />
              <small>{t("models.additionalLabelsHint")}</small>
            </label>
          </div>
        </div>
      </div>

      <div className="model-editor-section">
        <div className="model-editor-section-header">
          <span className="model-editor-section-number">4</span>
          <div>
            <h3>{t("models.performanceGuardrails")}</h3>
            <p>{t("models.performanceDescription")}</p>
          </div>
        </div>
        <div className="model-editor-section-body form-grid-2">
          <label className="field">
            <span>{t("models.contextWindow")}</span>
            <input type="number" min="1" value={contextWindow} onChange={(event) => setContextWindow(event.target.value)} placeholder={t("models.contextPlaceholder")} />
            <small>{t("models.contextHint")}</small>
          </label>
          <label className="field">
            <span>{t("models.targetLatency")}</span>
            <input type="number" min="0" value={targetLatency} onChange={(event) => setTargetLatency(event.target.value)} placeholder={t("models.latencyPlaceholder")} />
            <small>{t("models.latencyHint")}</small>
          </label>
        </div>
      </div>

      <div className="model-editor-section">
        <div className="model-editor-section-header">
          <span className="model-editor-section-number">5</span>
          <div>
            <h3>{t("models.tokenPricing")}</h3>
            <p>{t("models.tokenPricingDescription")}</p>
          </div>
        </div>
        <div className="model-editor-section-body form-grid-3">
          <label className="field">
            <span>{t("models.inputPriceField")}</span>
            <input type="number" min="0" step="any" value={inputPrice} onChange={(event) => setInputPrice(event.target.value)} placeholder="0.00" />
            <small>{t("models.inputPriceHint")}</small>
          </label>
          <label className="field">
            <span>{t("models.outputPriceField")}</span>
            <input type="number" min="0" step="any" value={outputPrice} onChange={(event) => setOutputPrice(event.target.value)} placeholder="0.00" />
            <small>{t("models.outputPriceHint")}</small>
          </label>
          <label className="field">
            <span>{t("models.pricingVersion")}</span>
            <input value={pricingVersion} onChange={(event) => setPricingVersion(event.target.value)} placeholder="1" />
            <small>{t("models.pricingVersionHint")}</small>
          </label>
        </div>
        {savedModel?.feature_metadata?.pricing && <p className="model-feature-conditions">{t("models.tieredPricingEditHint")}</p>}
      </div>

      <div className="form-actions model-editor-actions">
        {!onClose && <Link href="/models" className="gateway-button gateway-button--ghost">{t("common.cancel")}</Link>}
        <button className="gateway-button model-submit-btn" type="submit" disabled={working || !providerId || !name.trim() || !displayName.trim()}>
          {working ? t("common.saving") : savedModel ? t("models.saveMetadata") : t("models.saveInactive")}
        </button>
        {savedModel && <button className="gateway-button gateway-button--ghost" type="button" onClick={() => void runProbe()} disabled={working}>{working ? t("models.working") : probe || savedModel.last_probe_at ? t("models.probeAgain") : t("models.probeModel")}</button>}
        {savedModel && savedModel.status !== "active" && <button className="gateway-button" type="button" onClick={() => void setActive("active")} disabled={working || !canActivate}>{t("models.activate")}</button>}
        {savedModel?.status === "active" && <button className="gateway-button gateway-button--ghost" type="button" onClick={() => void setActive("inactive")} disabled={working}>{t("models.deactivate")}</button>}
      </div>
    </form>
    {savedModel && <p className="model-saved-info-bar">{t("models.savedModelId")} <code>{savedModel.id}</code> · {savedModel.status} · {modelHealthLabel(savedModel.health_status, language)}{probeVersion != null ? ` · ${t("models.probeCredential")} v${probeVersion}` : ""}</p>}
  </section>;
  return onClose
    ? <div className="gateway-modal-backdrop model-editor-backdrop"><section className="model-editor-dialog" role="dialog" aria-modal="true" aria-label={savedModel ? t("models.editModel") : t("models.add")}><ModelEditorDialogHeader title={savedModel ? t("models.editModel") : t("models.add")} onClose={onClose} />{editor}</section></div>
    : <>{editor}</>;
}

export function AddModelScene() {
  const { t } = useLanguage();
  return (
    <div className="add-model-layout">
      <section className="gateway-page-heading add-model-heading">
        <div>
          <p className="gateway-eyebrow">{t("models.platformCatalogue")}</p>
          <h1>{t("models.add")}</h1>
          <p>{t("models.addDescription")}</p>
        </div>
        <Link href="/models" className="gateway-button gateway-button--ghost">
          ← {t("models.back")}
        </Link>
      </section>
      <ModelMetadataEditor />
    </div>
  );
}
