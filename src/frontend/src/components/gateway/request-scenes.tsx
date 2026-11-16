"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useLanguage, type Language } from "@/components/language-provider";

import {
  getGatewayRequest,
  listGatewayRequests,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type {
  GatewayRequestDetail,
  GatewayRequestStatus,
  GatewayRequestSummary,
  GatewayRequestSortField,
} from "@/lib/gateway-types";
import { PaginationControls, TABLE_PAGE_SIZE } from "@/components/gateway/pagination-controls";
import { ListPageSkeleton } from "@/components/gateway/gateway-page-skeletons";
import { FilterBuilder, type FilterCondition } from "@/components/gateway/filter-builder";
import { SortPrioritySummary, SortableTableHeader, parseSortCriteria, sortQueryParams, toggleSortCriterion, type SortCriterion } from "@/components/gateway/multi-sort";

function apiMessage(error: unknown, fallback: string): string {
  return error && typeof error === "object" && "message" in error
    ? String((error as GatewayApiError).message)
    : fallback;
}

function statusTone(status: string): "healthy" | "watch" | "incident" | "neutral" {
  if (status === "completed") return "healthy";
  if (status === "partial") return "watch";
  if (status === "failed") return "incident";
  return "neutral";
}

function statusLabel(status: string, language: Language): string {
  if (status === "completed") return language === "vi" ? "Hoàn tất" : "Completed";
  if (status === "partial") return language === "vi" ? "Một phần" : "Partial";
  if (status === "failed") return language === "vi" ? "Thất bại" : "Failed";
  if (status === "pending") return language === "vi" ? "Đang chờ" : "Pending";
  return status || (language === "vi" ? "Không xác định" : "Unknown");
}

function formatNumber(value: number | undefined, digits = 0): string {
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(Number(value ?? 0));
}

function formatTimestamp(value: string | null | undefined, language: Language, notFinished = "Not finished"): string {
  if (!value) return notFinished;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(language === "vi" ? "vi-VN" : "en-US", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function value(record: Record<string, unknown> | undefined, key: string, fallback = "—"): string {
  const current = record?.[key];
  return current === undefined || current === null || current === "" ? fallback : String(current);
}

function canReadRequests(permissions: readonly string[]): boolean {
  return permissions.some((permission) => [
    "requests.read_own",
    "requests.read_global",
    "usage.read_own",
    "usage.read_global",
  ].includes(permission));
}

const requestSortFields: GatewayRequestSortField[] = ["started_at", "latency", "cost", "status", "source"];

export function RequestsScene({ permissions }: { permissions: readonly string[] }) {
  const { t, language } = useLanguage();
  const router = useRouter();
  const searchParams = useSearchParams();
  const initialFilterQuery = searchParams.get("filter") ?? "";
  const initialTokens = initialFilterQuery.match(/\S+/g) ?? [];
  const initialSearch = initialTokens.find((token) => token.startsWith("application:"))?.slice(12) || searchParams.get("search") || "";
  const initialStatus = initialTokens.find((token) => token.startsWith("status:"))?.slice(7) || searchParams.get("status") || "";
  const initialSource = initialTokens.find((token) => token.startsWith("source:"))?.slice(7) || searchParams.get("source") || "";
  const initialPage = Math.max(1, Number(searchParams.get("page") ?? "1") || 1);
  const initialSortCriteria = parseSortCriteria(searchParams.get("sort_by"), searchParams.get("sort_dir"), requestSortFields);
  const [filterQuery, setFilterQuery] = useState(initialFilterQuery || [initialSearch, initialStatus && `status:${initialStatus}`, initialSource && `source:${initialSource}`].filter(Boolean).join(" "));
  const [filters, setFilters] = useState<FilterCondition[]>([
    ...(initialSearch ? [{ id: "initial-application", field: "application", operator: "contains" as const, value: initialSearch }] : []),
    ...(initialStatus ? [{ id: "initial-status", field: "status", operator: "equals" as const, value: initialStatus }] : []),
    ...(initialSource ? [{ id: "initial-source", field: "source", operator: "equals" as const, value: initialSource }] : []),
  ]);
  const [search, setSearch] = useState(initialSearch);
  const [status, setStatus] = useState(initialStatus);
  const [source, setSource] = useState(initialSource);
  const [page, setPage] = useState(initialPage);
  const [sortCriteria, setSortCriteria] = useState<SortCriterion<GatewayRequestSortField>[]>(initialSortCriteria);
  const [items, setItems] = useState<GatewayRequestSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const pageSize = TABLE_PAGE_SIZE;
  const readable = canReadRequests(permissions);
  const [state, setState] = useState<"loading" | "ready" | "error">(readable ? "loading" : "ready");

  function syncUrl(next: { filter?: string; page?: number; sortCriteria?: SortCriterion<GatewayRequestSortField>[] }) {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("application_id");
    params.delete("search");
    params.delete("status");
    params.delete("source");
    const activeSortCriteria = next.sortCriteria ?? sortCriteria;
    const entries: Array<[string, string | undefined]> = [
      ["filter", next.filter ?? filterQuery],
      ["page", String(next.page ?? page)],
      ["sort_by", sortQueryParams(activeSortCriteria).sort_by?.join(",")],
      ["sort_dir", sortQueryParams(activeSortCriteria).sort_dir?.join(",")],
    ];
    entries.forEach(([key, current]) => {
      if (!current || (key === "page" && current === "1")) params.delete(key);
      else params.set(key, current);
    });
    const query = params.toString();
    router.replace(query ? `/requests?${query}` : "/requests", { scroll: false });
  }

  useEffect(() => {
    if (!readable) return;
    let active = true;
    void listGatewayRequests({
      search: search || undefined,
      status: (status || undefined) as GatewayRequestStatus | undefined,
      source: (source || undefined) as "playground" | "api" | "unknown" | undefined,
      ...sortQueryParams(sortCriteria),
      page,
      page_size: pageSize,
    }).then((response) => {
      if (!active) return;
      setItems(response.items);
      setTotal(response.total);
      setState("ready");
      setError(null);
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("error");
      setError(apiMessage(reason, t("requests.errorHistory")));
    });
    return () => { active = false; };
  }, [page, pageSize, readable, search, sortCriteria, source, status, t]);

  function updateFilters(next: FilterCondition[]) {
    setFilters(next);
    const application = next.find((item) => item.field === "application" && item.value.trim());
    const status = next.find((item) => item.field === "status" && item.value);
    const source = next.find((item) => item.field === "source" && item.value);
    const nextQuery = next.filter((item) => item.value).map((item) => `${item.field}:${item.value}`).join(" ");
    setFilterQuery(nextQuery);
    setSearch(application?.value.trim() ?? "");
    setStatus(status?.value ?? "");
    setSource(source?.value ?? "");
    setPage(1);
    syncUrl({ filter: nextQuery, page: 1 });
  }

  function updateSort(next: GatewayRequestSortField) {
    const nextCriteria = toggleSortCriterion(sortCriteria, next);
    setSortCriteria(nextCriteria);
    setPage(1);
    syncUrl({ sortCriteria: nextCriteria, page: 1 });
  }

  const sortLabels: Record<GatewayRequestSortField, string> = { status: t("requests.status"), source: t("requests.source"), latency: t("requests.latency"), cost: t("requests.cost"), started_at: t("requests.started") };

  if (state === "loading") return <ListPageSkeleton label={t("requests.loadingHistory")} filters={3} columns={7} />;

  return <div className="gateway-request-page">
    <section className="gateway-page-heading gateway-request-heading">
      <div>
        <h1>{t("requests.pageTitle")}</h1>
        <p>{t("requests.description")}</p>
      </div>
      <span className="gateway-request-heading-note">{t("requests.serverLedger")}</span>
    </section>
    {!readable && <div className="notice notice--warning" role="status">{t("requests.accessNotice")}</div>}
    {readable && <>
      <FilterBuilder fields={[{ key: "application", label: t("requests.applicationName"), type: "text" }, { key: "status", label: t("requests.status"), type: "enum", options: [{ value: "completed", label: t("requests.completed") }, { value: "partial", label: t("requests.partial") }, { value: "failed", label: t("requests.failed") }, { value: "pending", label: t("requests.pending") }] }, { key: "source", label: t("requests.source"), type: "enum", options: [{ value: "playground", label: t("requests.playground") }, { value: "api", label: t("requests.api") }, { value: "unknown", label: t("requests.unknown") }] }]} value={filters} onChange={updateFilters} />
      {error && <div className="notice notice--warning" role="status">{error}</div>}
      {state === "ready" && items.length === 0 && <div className="notice" role="status">{t("requests.noMatch")}</div>}
      {items.length > 0 && <section className="gateway-panel table-panel gateway-request-table-panel">
        <SortPrioritySummary criteria={sortCriteria} labels={sortLabels} />
        <div className="gateway-table-wrap">
          <table className="gateway-table gateway-request-table">
            <thead><tr><th>{t("requests.request")}</th><th>{t("requests.application")}</th><SortableTableHeader field="status" label={t("requests.status")} criteria={sortCriteria} onToggle={updateSort} /><th>{t("requests.aggregation")}</th><SortableTableHeader field="source" label={t("requests.source")} criteria={sortCriteria} onToggle={updateSort} /><SortableTableHeader field="latency" label={t("requests.latency")} criteria={sortCriteria} onToggle={updateSort} /><SortableTableHeader field="cost" label={t("requests.cost")} criteria={sortCriteria} onToggle={updateSort} /><SortableTableHeader field="started_at" label={t("requests.started")} criteria={sortCriteria} onToggle={updateSort} /></tr></thead>
            <tbody>{items.map((item) => <tr key={item.request_id}>
              <td className="request-table-identity"><Link className="gateway-table-link" href={`/requests/${encodeURIComponent(item.request_id)}`} title={item.request_id}>{item.request_id}</Link><small>{formatTimestamp(item.finished_at ?? item.started_at, language, t("requests.notFinished"))}</small></td>
              <td className="request-table-application">
                <strong title={item.application_name ?? item.application_id}>{item.application_name ?? item.application_id}</strong>
                {item.application_deleted && <span className="application-deleted-badge">{t("requests.deleted")}</span>}
                <small title={item.application_id}>{item.application_id}</small>
              </td>
              <td className="table-cell-nowrap"><span className={`gateway-status gateway-status--${statusTone(item.status)}`}><i className="status-pin" aria-hidden="true" />{statusLabel(item.status, language)}</span></td>
              <td className="table-cell-nowrap">{item.aggregation_status ?? t("requests.notAvailable")}</td>
              <td className="table-cell-nowrap">{item.source}</td>
              <td className="numeric table-cell-nowrap">{formatNumber(item.total_latency_ms, 0)}ms</td>
              <td className="numeric table-cell-nowrap">{formatNumber(item.total_cost, 4)}</td>
              <td className="table-cell-nowrap">{formatTimestamp(item.started_at, language, t("requests.notFinished"))}</td>
            </tr>)}</tbody>
          </table>
        </div>
        <PaginationControls page={page} pageSize={pageSize} total={total} onPageChange={(next) => { setPage(next); syncUrl({ page: next }); }} label={t("requests.pagination")} />
      </section>}
    </>}
  </div>;
}

function RequestStage({ index, title, detail, state = "complete" }: { index: string; title: string; detail: string; state?: "complete" | "active" | "empty" }) {
  return <article className={`request-trace-stage is-${state}`}><span>{index}</span><strong>{title}</strong><small>{detail}</small></article>;
}

function executionTone(execution: Record<string, unknown>): "healthy" | "fallback" | "incident" | "neutral" {
  const status = value(execution, "status", "unknown").toLowerCase();
  const fallback = execution.fallback === true || value(execution, "fallback", "false").toLowerCase() === "true";
  if (fallback) return "fallback";
  if (["failed", "error", "timeout", "cancelled"].includes(status)) return "incident";
  if (["completed", "succeeded", "success"].includes(status)) return "healthy";
  return "neutral";
}

export function RequestRoutingMap({
  detail,
  tasks,
  executions,
  aggregation,
}: {
  detail: GatewayRequestDetail;
  tasks: Array<Record<string, unknown>>;
  executions: Array<Record<string, unknown>>;
  aggregation?: Record<string, unknown>;
}) {
  const { t } = useLanguage();
  const aggregationStatus = value(aggregation, "status", detail.aggregation_status ?? "not recorded");
  const finalAnswerAvailable = Boolean(value(aggregation, "final_answer", detail.output ?? ""));
  const taskLookup = new Map(tasks.map((task, index) => [
    value(task, "task_id", value(task, "id", `task-${index + 1}`)),
    value(task, "description", value(task, "task_type", `Task ${index + 1}`)),
  ]));
  const executionCountLabel = t("requests.executionCount", { count: executions.length, suffix: executions.length === 1 ? "" : "s" });

  return <section className="request-routing-map" aria-labelledby="request-routing-map-title">
    <header className="request-routing-map-header">
      <div>
        <h2 id="request-routing-map-title">{t("requests.routingMap")}</h2>
        <p>{t("requests.routingDescription", { request: detail.request_id })}</p>
      </div>
      <div className="request-routing-map-meta" aria-label={t("requests.routeSummary")}>
        <span className="request-routing-map-badge"><i aria-hidden="true" />{t("requests.actualPath")}</span>
        <span>{executionCountLabel}</span>
      </div>
    </header>
    <div className="request-routing-map-canvas">
      <section className="request-routing-map-lane" aria-labelledby="request-routing-intake-label">
        <span className="request-routing-map-lane-label" id="request-routing-intake-label">01 / {t("requests.intake")}</span>
        <article className="request-routing-node request-routing-node--intake">
          <span className="request-routing-node-mark" aria-hidden="true" />
          <div><strong>{detail.source || t("requests.unknown")}</strong><small>{t("requests.promptAccepted")}</small><code translate="no">{detail.request_id}</code></div>
        </article>
      </section>
      <span className="request-routing-map-connector" aria-hidden="true" />
      <section className="request-routing-map-lane" aria-labelledby="request-routing-core-label">
        <span className="request-routing-map-lane-label" id="request-routing-core-label">02 / {t("requests.routingCore")}</span>
        <article className="request-routing-node request-routing-node--core">
          <span className="request-routing-node-mark" aria-hidden="true" />
          <div><strong>{t("requests.gatewayPolicy")}</strong><small>{tasks.length ? t("requests.persistedTask", { count: tasks.length, suffix: tasks.length === 1 ? "node" : "nodes" }) : t("requests.noTaskRecords")}</small><code>{detail.fallback_used ? t("requests.fallbackAvailable") : t("requests.primaryRoute")}</code></div>
        </article>
        {tasks.length > 0 && <ul className="request-routing-task-chips" aria-label={t("requests.persistedTaskGraph")}>{tasks.slice(0, 4).map((task, index) => {
          const taskId = value(task, "task_id", value(task, "id", `task-${index + 1}`));
          return <li key={taskId}><span>{String(index + 1).padStart(2, "0")}</span>{value(task, "description", taskId)}</li>;
        })}</ul>}
        {tasks.length > 4 && <small className="request-routing-map-overflow">{t("requests.moreTasks", { count: tasks.length - 4 })}</small>}
      </section>
      <span className="request-routing-map-connector" aria-hidden="true" />
      <section className="request-routing-map-lane" aria-labelledby="request-routing-execution-label">
        <span className="request-routing-map-lane-label" id="request-routing-execution-label">03 / {t("requests.modelExecution")}</span>
        {executions.length > 0 ? <ol className="request-routing-execution-list">{executions.slice(0, 6).map((execution, index) => {
          const tone = executionTone(execution);
          const taskId = value(execution, "task_id", "");
          const taskLabel = taskLookup.get(taskId) ?? (taskId || `Route ${index + 1}`);
          const model = value(execution, "model_name", value(execution, "model", t("requests.modelNotRecorded")));
          const provider = value(execution, "provider_name", value(execution, "provider", t("requests.providerNotRecorded")));
          return <li className={`request-routing-node request-routing-node--${tone}`} key={`${taskId}-${model}-${index}`}>
            <span className="request-routing-node-mark" aria-hidden="true" />
            <div><strong translate="no">{model}</strong><small translate="no">{provider} / {value(execution, "status", t("requests.statusUnknown"))}</small><code>{taskLabel}</code></div>
          </li>;
        })}</ol> : <article className="request-routing-node request-routing-node--empty"><span className="request-routing-node-mark" aria-hidden="true" /><div><strong>{t("requests.noExecution")}</strong><small>{t("requests.noModelRoute")}</small></div></article>}
        {executions.length > 6 && <small className="request-routing-map-overflow">{t("requests.moreExecutions", { count: executions.length - 6 })}</small>}
      </section>
      <span className="request-routing-map-connector" aria-hidden="true" />
      <section className="request-routing-map-lane" aria-labelledby="request-routing-aggregate-label">
        <span className="request-routing-map-lane-label" id="request-routing-aggregate-label">04 / {t("requests.aggregate")}</span>
        <article className={`request-routing-node request-routing-node--aggregate${aggregation ? "" : " request-routing-node--empty"}`}>
          <span className="request-routing-node-mark" aria-hidden="true" />
          <div><strong>{aggregation ? value(aggregation, "strategy", t("requests.aggregationResult")) : t("requests.noAggregation")}</strong><small>{aggregationStatus}</small><code>{aggregation ? (finalAnswerAvailable ? t("requests.finalReady") : t("requests.resultIncomplete")) : t("requests.notPersisted")}</code></div>
        </article>
      </section>
    </div>
    <footer className="request-routing-map-footer"><span><i className="is-healthy" aria-hidden="true" />{t("requests.primaryPath")}</span><span><i className="is-fallback" aria-hidden="true" />{t("requests.fallbackExecution")}</span><span><i className="is-incident" aria-hidden="true" />{t("requests.failedTimedOut")}</span></footer>
  </section>;
}

export function RequestDetailScene({ id }: { id?: string }) {
  const { t, language } = useLanguage();
  const [detail, setDetail] = useState<GatewayRequestDetail | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let active = true;
    void getGatewayRequest(id).then((value) => {
      if (!active) return;
      setDetail(value);
      setState("ready");
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("error");
      setError(apiMessage(reason, t("requests.errorHistory")));
    });
    return () => { active = false; };
  }, [id, t]);

  if (state === "loading") return <ListPageSkeleton label={t("requests.loadingTrace")} filters={0} columns={5} />;
  if (state === "error" || !detail) return <div className="notice notice--warning" role="status">{error ?? t("requests.notFound")}</div>;

  const aggregation = detail.aggregation ?? undefined;
  const tasks = detail.tasks ?? [];
  const traces = detail.traces ?? [];
  const executions = detail.executions ?? [];
  const hasDecomposition = tasks.length > 1 || traces.some((trace) => value(trace, "stage").toLowerCase().includes("decom"));
  const aggregationStrategy = value(aggregation, "strategy", t("requests.notRecorded"));
  const contributions = Array.isArray(aggregation?.contributions_json) ? aggregation.contributions_json : [];

  return <div className="gateway-request-detail-page">
    <Link className="gateway-back-link" href="/requests">{t("requests.backToRequests")}</Link>
    <section className="gateway-page-heading gateway-request-detail-heading">
      <div><h1>{t("requests.trace")}</h1><p>{detail.request_id} / {detail.application_name ?? detail.application_id}{detail.application_deleted && <> <span className="application-deleted-badge">{t("requests.deleted")}</span></>} / {detail.source}</p></div>
      <span className={`gateway-status gateway-status--${statusTone(detail.status)}`}><i className="status-pin" aria-hidden="true" />{statusLabel(detail.status, language)}</span>
    </section>
    <dl className="request-detail-meta gateway-panel">
      <div><dt>{t("requests.startedMeta")}</dt><dd>{formatTimestamp(detail.started_at, language, t("requests.notFinished"))}</dd></div>
      <div><dt>{t("requests.finishedMeta")}</dt><dd>{formatTimestamp(detail.finished_at, language, t("requests.notFinished"))}</dd></div>
      <div><dt>{t("requests.latency")}</dt><dd>{formatNumber(detail.latency_ms, 0)}ms</dd></div>
      <div><dt>{t("requests.estimatedCost")}</dt><dd>{formatNumber(detail.estimated_cost, 4)}</dd></div>
      <div><dt>{t("requests.modelsUsed")}</dt><dd>{detail.models_used?.join(" / ") || detail.model || t("requests.notRecorded")}</dd></div>
      <div><dt>{t("requests.fallback")}</dt><dd>{detail.fallback_used ? detail.fallback_reason ?? t("requests.used") : t("requests.notUsed")}</dd></div>
    </dl>
    <RequestRoutingMap detail={detail} tasks={tasks} executions={executions} aggregation={aggregation} />
    <section className="request-trace-layout">
      <section className="gateway-panel request-trace-panel" aria-labelledby="request-path-title">
        <div className="gateway-section-heading"><div><h2 id="request-path-title">{t("requests.requestPath")}</h2><p>{t("requests.requestPathDescription")}</p></div><span>{t("requests.taskCount", { tasks: tasks.length, executions: executions.length })}</span></div>
        <div className="request-trace-lineup">
          <RequestStage index="01" title={t("requests.intake")} detail={detail.prompt ? t("requests.promptAccepted") : t("requests.inputRecorded")} />
          <RequestStage index="02" title={t("requests.understand")} detail={traces.find((trace) => value(trace, "stage").toLowerCase().includes("understand")) ? t("requests.semanticAnalysis") : t("requests.deterministicPath")} />
          <RequestStage index="03" title={t("requests.decompose")} detail={hasDecomposition ? t("requests.taskGraphNodes", { count: tasks.length }) : t("requests.skippedSingleTask")} state={hasDecomposition ? "complete" : "empty"} />
          <RequestStage index="04" title={t("requests.execute")} detail={executions.length ? t("requests.modelExecutions", { count: executions.length }) : t("requests.noExecution")} state={executions.length ? "complete" : "empty"} />
          <RequestStage index="05" title={t("requests.aggregate")} detail={aggregation ? `${aggregationStrategy} / ${value(aggregation, "status")}` : t("requests.noResultRecorded")} state={aggregation ? "complete" : "empty"} />
        </div>
        <div className="request-task-list"><h3>{t("requests.taskExecutions")}</h3>{tasks.length === 0 ? <p className="request-empty-copy">{t("requests.noTaskRecordsDetail")}</p> : tasks.map((task, index) => {
          const taskId = value(task, "task_id", value(task, "id", `task-${index + 1}`));
          const execution = executions.find((item) => value(item, "task_id") === taskId);
          return <article key={taskId}><div><span>{t("requests.task", { index: String(index + 1).padStart(2, "0") })}</span><strong>{value(task, "description", taskId)}</strong><small>{value(task, "task_type", t("requests.gatewayTask"))} / {value(task, "expected_output", t("requests.outputRecorded"))}</small></div><div><b>{value(execution, "model_name", value(execution, "model", t("requests.notRouted")))}</b><small>{value(execution, "status", t("requests.noExecutionDetail"))} / {formatNumber(Number(execution?.latency_ms), 0)}ms / {formatNumber(Number(execution?.cost_usd), 4)}</small></div></article>;
        })}</div>
      </section>
      <aside className="gateway-panel request-aggregation-panel" aria-labelledby="aggregation-title">
        <div className="gateway-section-heading"><div><h2 id="aggregation-title">{t("requests.aggregationResult")}</h2><p>{t("requests.aggregationDescription")}</p></div></div>
        <dl className="request-aggregation-facts"><div><dt>{t("requests.strategy")}</dt><dd>{aggregationStrategy}</dd></div><div><dt>{t("requests.status")}</dt><dd>{value(aggregation, "status", detail.aggregation_status ?? t("requests.notRecorded"))}</dd></div><div><dt>{t("requests.missingGoals")}</dt><dd>{Array.isArray(aggregation?.missing_goal_ids_json) ? aggregation.missing_goal_ids_json.length : 0}</dd></div></dl>
        <div className="request-aggregation-reason"><span>{t("requests.reason")}</span><p>{value(aggregation, "reason", t("requests.noAggregationPersisted"))}</p></div>
        <div className="request-final-answer"><span>{t("requests.finalResponse")}</span><p>{value(aggregation, "final_answer", detail.output ?? t("requests.noFinalResponse"))}</p></div>
        {contributions.length > 0 && <div className="request-contributions"><span>{t("requests.contributions")}</span><ul>{contributions.map((contribution, index) => <li key={index}>{typeof contribution === "object" && contribution !== null ? value(contribution as Record<string, unknown>, "excerpt", value(contribution as Record<string, unknown>, "task_id", `Contribution ${index + 1}`)) : String(contribution)}</li>)}</ul></div>}
      </aside>
    </section>
  </div>;
}
