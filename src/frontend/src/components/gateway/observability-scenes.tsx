"use client";

import { useEffect, useState } from "react";

import {
  exportGatewayComparisonsCsv,
  exportUsageCsv,
  getGatewayComparisonReport,
  getOverview,
  getUsage,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type {
  ComparisonReport,
  GatewayRequestStatus,
  OverviewResponse,
  UsageResponse,
} from "@/lib/gateway-types";
import { PaginationControls, TABLE_PAGE_SIZE, useClientTablePagination } from "@/components/gateway/pagination-controls";
import { DashboardSkeleton, ListPageSkeleton } from "@/components/gateway/gateway-page-skeletons";
import { useLanguage } from "@/components/language-provider";

function apiMessage(error: unknown, fallback: string): string {
  return error && typeof error === "object" && "message" in error
    ? String((error as GatewayApiError).message)
    : fallback;
}

function State({ message, error = false }: { message: string; error?: boolean }) {
  return <div className={error ? "notice notice--warning" : "notice"} role="status">{message}</div>;
}

function number(value: number, digits = 0): string {
  return value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "Not set";
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(value);
}

function budgetStateLabel(state: string): string {
  if (state === "warning") return "Warning threshold reached";
  if (state === "exhausted") return "Budget exhausted";
  if (state === "healthy") return "Within budget";
  return "Not configured";
}

function signalTone(status: string): "healthy" | "watch" | "incident" {
  if (status === "completed") return "healthy";
  if (status === "failed") return "incident";
  return "watch";
}

function signalLabel(status: string, language: "vi" | "en" = "en"): string {
  if (status === "completed") return language === "vi" ? "Hoàn tất" : "Completed";
  if (status === "failed") return language === "vi" ? "Thất bại" : "Failed";
  if (status === "partial") return language === "vi" ? "Một phần" : "Partial";
  return status || (language === "vi" ? "Không rõ" : "Unknown");
}

function providerLabel(provider: string | null | undefined): string {
  const normalized = provider?.trim().toLowerCase();
  if (!normalized) return "—";
  const labels: Record<string, string> = {
    openai: "OpenAI",
    google: "Gemini",
    gemini: "Gemini",
    openrouter: "OpenRouter",
    anthropic: "Anthropic",
    typesafe: "TypeSafe",
    local: "Local",
    offline: "Offline",
  };
  return labels[normalized] ?? normalized.replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function DashboardScene({ permissions }: { permissions: readonly string[] }) {
  const { language } = useLanguage();
  const c = (vi: string, en: string) => language === "vi" ? vi : en;
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const scope = permissions.includes("usage.read_global") ? "global" : "own";

  useEffect(() => {
    let active = true;
    void getOverview({ scope }).then((value) => {
      if (!active) return;
      setOverview(value);
      setState("ready");
      setError(null);
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("error");
      setError(apiMessage(reason, language === "vi" ? "Không thể tải dữ liệu quan sát." : "Unable to load observability data."));
    });
    return () => { active = false; };
  }, [language, scope]);

  if (state === "loading") return <DashboardSkeleton label={c("Đang tải tổng quan trực tiếp", "Loading live overview")} />;

  return (
    <div className="gateway-overview-page">
      <section className="gateway-overview-header">
        <div>
          <h1>{c("Gateway đang vận hành.", "Gateway, in motion.")}</h1>
          <p>{c("Theo dõi request đi từ intake đến model execution, với runtime ledger đã lưu là nguồn dữ liệu chuẩn.", "See how requests move from intake to model execution, with the persisted runtime ledger as the source of truth.")}</p>
        </div>
        <span className="gateway-overview-scope"><i aria-hidden="true" />{scope === "global" ? c("Phạm vi toàn platform", "Global scope") : c("Workspace của bạn", "Your workspace")}</span>
      </section>
      {state === "error" && <State message={error ?? c("Không thể tải tổng quan.", "Unable to load overview.")} error />}
      {state === "ready" && overview && <>
        <section className="gateway-overview-command" aria-labelledby="overview-flow-title">
          <div className="gateway-overview-flow">
            <header className="gateway-overview-command-header">
              <div>
                <h2 id="overview-flow-title">{c("Luồng thực thi", "Runtime flow")}</h2>
                <p>{c("Dữ liệu quan sát đã lưu · phạm vi đang chọn", "Persisted telemetry · selected scope")}</p>
              </div>
              <span className="gateway-status gateway-status--healthy"><i className="status-pin" aria-hidden="true" />{c("Đã kết nối ledger", "Ledger connected")}</span>
            </header>
            <div className="gateway-overview-stages" role="group" aria-label={c("Luồng yêu cầu Gateway", "Gateway request flow")}>
              <article>
                <span className="gateway-overview-stage-index">01</span>
                <strong>{c("Tiếp nhận yêu cầu", "Request intake")}</strong>
                <small>{number(overview.total_requests)} {c("request đã route", "routed requests")}</small>
              </article>
              <span className="gateway-overview-connector" aria-hidden="true" />
              <article>
                <span className="gateway-overview-stage-index">02</span>
                <strong>{c("Cổng chính sách", "Policy gate")}</strong>
                <small>{number(overview.success_rate * 100, 1)}% {c("hoàn tất", "completed")}</small>
              </article>
              <span className="gateway-overview-connector" aria-hidden="true" />
              <article>
                <span className="gateway-overview-stage-index">03</span>
                <strong>{c("Thực thi model", "Model execution")}</strong>
                <small>{number(overview.active_model_count)} {c("model đang hoạt động", "active models")}</small>
              </article>
            </div>
            <div className="gateway-overview-flow-footer"><span>{c("Một control surface cho routing, cost và latency.", "One control surface for routing, cost and latency.")}</span><b>{number(overview.average_latency_ms, 0)}ms {c("latency trung bình", "avg latency")}</b></div>
          </div>
          <aside className="gateway-overview-rail" aria-label={c("Chỉ số tổng quan", "Overview metrics")}>
            <div className="gateway-overview-rail-heading"><span>{c("Số liệu hiện tại", "Current readout")}</span><small>{c("Sổ cái thực thi", "Runtime ledger")}</small></div>
            <dl>
              <div><dt>{c("Requests đã route", "Requests routed")}</dt><dd>{number(overview.total_requests)}</dd></div>
              <div><dt>{c("Tỷ lệ thành công", "Success rate")}</dt><dd>{number(overview.success_rate * 100, 1)}%</dd></div>
              <div><dt>{c("Latency trung bình", "Average latency")}</dt><dd>{number(overview.average_latency_ms, 0)}ms</dd></div>
              <div><dt>{c("Chi phí ước tính", "Estimated cost")}</dt><dd>{number(overview.total_cost, 4)}</dd></div>
            </dl>
            <div className="gateway-overview-rail-note"><span>{c("Độ phủ catalog", "Catalog coverage")}</span><strong>{number(overview.active_model_count)} {c("model khả dụng", "models available")}</strong></div>
          </aside>
        </section>
        <section className="gateway-panel gateway-overview-activity" aria-labelledby="overview-activity-title">
            <div className="gateway-section-heading">
            <div><h2 id="overview-activity-title">{c("Hoạt động gần đây", "Recent activity")}</h2><p>{c("Các tín hiệu request mới nhất được gateway ghi nhận.", "Latest request signals recorded by the gateway.")}</p></div>
            <span>{overview.recent_signals.length} {c("tín hiệu", "signals")}</span>
          </div>
          {overview.recent_signals.length === 0 ? <State message={c("Chưa có request nào được ghi nhận.", "No requests have been recorded yet.")} /> : <ol className="gateway-overview-signals">
            {overview.recent_signals.map((signal, index) => {
              const status = String(signal.status ?? "unknown");
              const tone = signalTone(status);
              return <li key={`${String(signal.request_id)}-${index}`}>
                <span className={`gateway-overview-signal-marker gateway-status--${tone}`} aria-hidden="true" />
                <div><strong>{signalLabel(status, language)}</strong><p>{String(signal.source ?? "unknown")} · {String(signal.request_id ?? "request")}</p></div>
                <span className="gateway-overview-signal-metrics">{number(Number(signal.latency_ms ?? 0), 0)}ms <i aria-hidden="true">·</i> {number(Number(signal.cost ?? 0), 4)} credits</span>
              </li>;
            })}
          </ol>}
        </section>
      </>}
    </div>
  );
}

export function UsageScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const [applicationId, setApplicationId] = useState("");
  const [model, setModel] = useState("");
  const [provider, setProvider] = useState("");
  const [source, setSource] = useState("");
  const [status, setStatus] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(1);
  const pageSize = TABLE_PAGE_SIZE;
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [comparisonReport, setComparisonReport] = useState<ComparisonReport | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportingComparisons, setExportingComparisons] = useState(false);
  const scope = permissions.includes("usage.read_global") ? "global" : "own";
  const invalidRange = Boolean(from && to && from > to);
  const comparisonPagination = useClientTablePagination(
    comparisonReport?.cohorts ?? [],
    comparisonReport ? `${applicationId}:${comparisonReport.comparisons}` : applicationId,
  );

  useEffect(() => {
    if (invalidRange) return;
    let active = true;
    void getUsage({
      application_id: applicationId || undefined,
      model: model || undefined,
      provider: provider || undefined,
      source: (source || undefined) as "playground" | "api" | "unknown" | undefined,
      status: (status || undefined) as GatewayRequestStatus | undefined,
      scope,
      from: from || undefined,
      to: to || undefined,
      page,
      page_size: pageSize,
    }).then((value) => {
      if (!active) return;
      setUsage(value);
      setState("ready");
      setError(null);
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("error");
      setError(apiMessage(reason, t("usage.loadError")));
    });
    return () => { active = false; };
  }, [applicationId, from, invalidRange, model, page, pageSize, provider, scope, source, status, t, to]);

  useEffect(() => {
    let active = true;
    void getGatewayComparisonReport({ application_id: applicationId || undefined })
      .then((value) => {
        if (active) setComparisonReport(value);
      })
      .catch(() => {
        if (active) setComparisonReport(null);
      });
    return () => { active = false; };
  }, [applicationId]);

  function resetPage(action: () => void) {
    action();
    setPage(1);
  }

  async function download() {
    setExporting(true);
    setError(null);
    try {
      const blob = await exportUsageCsv({ application_id: applicationId || undefined, model: model || undefined, provider: provider || undefined, source: (source || undefined) as "playground" | "api" | "unknown" | undefined, status: (status || undefined) as GatewayRequestStatus | undefined, scope, from: from || undefined, to: to || undefined });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "usage.csv";
      link.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(apiMessage(reason, t("usage.exportError")));
    } finally {
      setExporting(false);
    }
  }

  async function downloadComparisons() {
    setExportingComparisons(true);
    setError(null);
    try {
      const blob = await exportGatewayComparisonsCsv({ application_id: applicationId || undefined });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "gateway-comparisons.csv";
      link.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(apiMessage(reason, t("usage.exportError")));
    } finally {
      setExportingComparisons(false);
    }
  }

  if (state === "loading" && !invalidRange) return <ListPageSkeleton label={t("usage.title")} filters={7} columns={9} />;

  return (
    <div className="gateway-usage-page">
      <section className="gateway-page-heading"><div><p className="gateway-eyebrow">{scope === "global" ? t("usage.platformTelemetry") : t("usage.telemetry")}</p><h1>{t("usage.title")}</h1><p>{t("usage.description")}</p></div><button className="gateway-button gateway-button--secondary" type="button" onClick={download} disabled={exporting || invalidRange}>{exporting ? t("usage.preparing") : t("usage.export")}</button></section>
      <section className="filter-bar" aria-label={t("usage.filters")}><label><span>{t("usage.applicationId")}</span><input value={applicationId} onChange={(event) => resetPage(() => setApplicationId(event.target.value))} placeholder={t("usage.allApplications")} /></label><label><span>{t("usage.model")}</span><input value={model} onChange={(event) => resetPage(() => setModel(event.target.value))} placeholder={t("usage.allModels")} /></label><label><span>{t("usage.provider")}</span><input value={provider} onChange={(event) => resetPage(() => setProvider(event.target.value))} placeholder={t("usage.allProviders")} /></label><label><span>{t("usage.source")}</span><select value={source} onChange={(event) => resetPage(() => setSource(event.target.value))}><option value="">{t("usage.apiPlayground")}</option><option value="api">API</option><option value="playground">Playground</option></select></label><label><span>{t("usage.status")}</span><select value={status} onChange={(event) => resetPage(() => setStatus(event.target.value))}><option value="">{t("usage.allStatuses")}</option><option value="completed">{t("usage.completed")}</option><option value="partial">{t("usage.partial")}</option><option value="failed">{t("usage.failed")}</option></select></label><label><span>{t("usage.from")}</span><input type="date" value={from} onChange={(event) => resetPage(() => setFrom(event.target.value))} /></label><label><span>{t("usage.to")}</span><input type="date" value={to} onChange={(event) => resetPage(() => setTo(event.target.value))} /></label></section>
      {invalidRange && <State message={t("usage.invalidRange")} error />}
      {error && <State message={error} error />}
      {comparisonReport && <section className="gateway-panel table-panel"><div className="gateway-section-heading"><div><p className="gateway-eyebrow">{t("usage.comparisonCohorts")}</p><h2>{t("usage.gatewayVersusDirect")}</h2><p>{number(comparisonReport.comparisons)} {t("usage.comparisons")} · {t("usage.evaluatorSpend")} ${number(comparisonReport.evaluation_cost_usd, 4)}</p></div><button className="gateway-button gateway-button--secondary" type="button" onClick={downloadComparisons} disabled={exportingComparisons}>{exportingComparisons ? t("usage.preparing") : t("usage.exportComparisons")}</button></div>{comparisonReport.cohorts.length === 0 ? <State message={t("usage.noCohorts")} /> : <><div className="gateway-table-wrap"><table className="gateway-table"><thead><tr><th>{t("usage.referenceModel")}</th><th>{t("usage.samples")}</th><th>{t("usage.costDelta")}</th><th>{t("usage.gatewayLatency")}</th><th>{t("usage.directLatency")}</th><th>{t("usage.quality")}</th></tr></thead><tbody>{comparisonPagination.pageItems.map((cohort) => <tr key={cohort.reference_model_id}><td><strong>{cohort.reference_model_id}</strong></td><td>{number(cohort.completed_pairs)} / {number(cohort.samples)}</td><td>{cohort.cost_delta_usd == null ? "N/A" : `$${number(cohort.cost_delta_usd, 4)} (${number(cohort.cost_delta_percent ?? 0, 1)}%)`}</td><td>{cohort.gateway_latency_p50_ms == null ? "N/A" : `${number(cohort.gateway_latency_p50_ms)} / ${number(cohort.gateway_latency_p95_ms ?? 0)}ms`}</td><td>{cohort.reference_latency_p50_ms == null ? "N/A" : `${number(cohort.reference_latency_p50_ms)} / ${number(cohort.reference_latency_p95_ms ?? 0)}ms`}</td><td>{cohort.gateway_quality_mean == null ? "N/A" : `${number(cohort.gateway_quality_mean, 1)} / ${number(cohort.reference_quality_mean ?? 0, 1)}`}</td></tr>)}</tbody></table></div><PaginationControls page={comparisonPagination.page} pageSize={TABLE_PAGE_SIZE} total={comparisonReport.cohorts.length} onPageChange={comparisonPagination.setPage} label={t("usage.comparisonPagination")} /></>}</section>}
      {state === "ready" && usage && <><section className="gateway-metric-grid"><article className="gateway-metric"><span>{t("usage.totalRequests")}</span><strong>{number(usage.summary.requests)}</strong><small>{t("usage.filteredSet")}</small></article><article className="gateway-metric"><span>{t("usage.tokenVolume")}</span><strong>{number(usage.summary.total_tokens)}</strong><small>{t("usage.inputOutput")}</small></article><article className="gateway-metric"><span>{t("usage.estimatedSpend")}</span><strong>{number(usage.summary.total_cost, 4)}</strong><small>{t("usage.recordedCost")}</small></article></section>{usage.items.length === 0 ? <State message={t("usage.noRows")} /> : <section className="gateway-panel table-panel"><div className="gateway-table-wrap"><table className="gateway-table usage-ledger-table"><thead><tr><th>{t("usage.timestamp")}</th><th>{t("usage.application")}</th><th>{t("usage.provider")}</th><th>{t("usage.source")}</th><th>{t("usage.status")}</th><th>{t("usage.tokens")}</th><th>{t("usage.cost")}</th><th>{t("usage.latency")}</th><th>{t("usage.fallback")}</th></tr></thead><tbody>{usage.items.map((row, index) => <tr key={`${row.request_id}-${row.timestamp}-${index}`}><td className="table-cell-nowrap">{new Date(row.timestamp).toLocaleString()}</td><td className="usage-application-cell"><strong>{row.application}</strong></td><td className="usage-provider-cell"><span>{providerLabel(row.provider)}</span></td><td className="table-cell-nowrap">{row.source}</td><td className="table-cell-nowrap"><span className={`gateway-status gateway-status--${signalTone(row.status)}`}><span className="status-pin" aria-hidden="true" />{signalLabel(row.status)}</span></td><td className="table-cell-nowrap numeric">{number(row.tokens)}</td><td className="table-cell-nowrap numeric">{number(row.cost, 4)}</td><td className="table-cell-nowrap numeric">{number(row.latency_ms, 0)}ms</td><td className="table-cell-nowrap">{row.fallback_used ? t("usage.yes") : t("usage.no")}</td></tr>)}</tbody></table></div><PaginationControls page={usage.page} pageSize={usage.page_size} total={usage.total} onPageChange={setPage} label={t("usage.pagination")} /></section>}</>}
      {comparisonReport && <section className="gateway-panel table-panel"><div className="gateway-section-heading"><div><p className="gateway-eyebrow">Live comparison cohorts</p><h2>Gateway versus direct full-prompt models</h2><p>{number(comparisonReport.comparisons)} comparisons · evaluator spend ${number(comparisonReport.evaluation_cost_usd, 4)}</p></div><button className="gateway-button gateway-button--secondary" type="button" onClick={downloadComparisons} disabled={exportingComparisons}>{exportingComparisons ? "Preparing CSV…" : "Export comparisons"}</button></div>{comparisonReport.cohorts.length === 0 ? <State message="No live comparison cohorts are available yet." /> : <><div className="gateway-table-wrap"><table className="gateway-table"><thead><tr><th>Reference model</th><th>Samples</th><th>Cost delta</th><th>Gateway latency p50 / p95</th><th>Direct latency p50 / p95</th><th>Quality Gateway / direct</th></tr></thead><tbody>{comparisonPagination.pageItems.map((cohort) => <tr key={cohort.reference_model_id}><td><strong>{cohort.reference_model_id}</strong></td><td>{number(cohort.completed_pairs)} / {number(cohort.samples)}</td><td>{cohort.cost_delta_usd == null ? "N/A" : `$${number(cohort.cost_delta_usd, 4)} (${number(cohort.cost_delta_percent ?? 0, 1)}%)`}</td><td>{cohort.gateway_latency_p50_ms == null ? "N/A" : `${number(cohort.gateway_latency_p50_ms)} / ${number(cohort.gateway_latency_p95_ms ?? 0)}ms`}</td><td>{cohort.reference_latency_p50_ms == null ? "N/A" : `${number(cohort.reference_latency_p50_ms)} / ${number(cohort.reference_latency_p95_ms ?? 0)}ms`}</td><td>{cohort.gateway_quality_mean == null ? "N/A" : `${number(cohort.gateway_quality_mean, 1)} / ${number(cohort.reference_quality_mean ?? 0, 1)}`}</td></tr>)}</tbody></table></div><PaginationControls page={comparisonPagination.page} pageSize={TABLE_PAGE_SIZE} total={comparisonReport.cohorts.length} onPageChange={comparisonPagination.setPage} label="Comparison cohorts pagination" /></>}</section>}
      {state === "ready" && usage && <><section className="gateway-metric-grid"><article className="gateway-metric"><span>Total requests</span><strong>{number(usage.summary.requests)}</strong><small>Filtered request set</small></article><article className="gateway-metric"><span>Token volume</span><strong>{number(usage.summary.total_tokens)}</strong><small>Input + output tokens</small></article><article className="gateway-metric"><span>Estimated spend</span><strong>{number(usage.summary.total_cost, 4)}</strong><small>Filtered usage cost</small></article>{usage.budget && <article className="gateway-metric gateway-metric--budget"><span>Current-month budget</span><strong>{money(usage.budget.remaining_usd)}</strong><small>{money(usage.budget.spent_usd)} spent of {money(usage.budget.limit_usd)} · {budgetStateLabel(usage.budget.state)}</small></article>}</section>{usage.items.length === 0 ? <State message="No usage rows match these filters." /> : <section className="gateway-panel table-panel"><div className="gateway-table-wrap"><table className="gateway-table usage-ledger-table"><thead><tr><th>Timestamp</th><th>Application</th><th>{t("usage.provider")}</th><th>Source</th><th>Status</th><th>Tokens</th><th>Cost</th><th>Latency</th><th>Fallback</th></tr></thead><tbody>{usage.items.map((row, index) => <tr key={`${row.request_id}-${row.timestamp}-${index}`}><td className="table-cell-nowrap">{new Date(row.timestamp).toLocaleString()}</td><td className="usage-application-cell"><strong>{row.application}</strong></td><td className="usage-provider-cell"><span>{providerLabel(row.provider)}</span></td><td className="table-cell-nowrap">{row.source}</td><td className="table-cell-nowrap"><span className={`gateway-status gateway-status--${signalTone(row.status)}`}><span className="status-pin" aria-hidden="true" />{signalLabel(row.status)}</span></td><td className="table-cell-nowrap numeric">{number(row.tokens)}</td><td className="table-cell-nowrap numeric">{number(row.cost, 4)}</td><td className="table-cell-nowrap numeric">{number(row.latency_ms, 0)}ms</td><td className="table-cell-nowrap">{row.fallback_used ? "Yes" : "No"}</td></tr>)}</tbody></table></div><PaginationControls page={usage.page} pageSize={usage.page_size} total={usage.total} onPageChange={setPage} label="Usage pagination" /></section>}</>}
    </div>
  );
}
