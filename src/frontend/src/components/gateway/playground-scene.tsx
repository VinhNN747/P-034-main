"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { useLanguage } from "@/components/language-provider";

import { buildPlaygroundGatewayRequest } from "@/components/gateway/playground-request";
import { PlaygroundCompareControls } from "@/components/gateway/playground-compare-controls";
import { MarkdownRenderer } from "@/components/gateway/markdown-renderer";
import { toPlaygroundViewModel } from "@/components/gateway/playground-view-model";
import { PlaygroundSkeleton } from "@/components/gateway/gateway-page-skeletons";
import { PaginationControls, TABLE_PAGE_SIZE, useClientTablePagination } from "@/components/gateway/pagination-controls";
import { canCompareRouting, comparisonIsTerminal, comparisonProgress } from "@/components/gateway/playground-comparison";
import {
  createGatewayRequest,
  createGatewayComparison,
  getApplication,
  getGatewayRequest,
  getGatewayComparison,
  getOverview,
  getUsage,
  listAvailableModels,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type { ApplicationDetail, ComparisonRun, GatewayRequestDetail, ModelSummary } from "@/lib/gateway-types";

function messageFor(error: unknown): string {
  return error && typeof error === "object" && "message" in error
    ? String((error as GatewayApiError).message)
    : "The gateway request could not be completed.";
}

function State({ message, error = false }: { message: string; error?: boolean }) {
  return <div className={error ? "notice notice--warning" : "notice"} role="status">{message}</div>;
}

async function listAllActiveModels(): Promise<ModelSummary[]> {
  const pageSize = 100;
  const firstPage = await listAvailableModels({ status: "active", page: 1, page_size: pageSize });
  const models = [...firstPage.items];
  const pageCount = Math.ceil(firstPage.total / firstPage.page_size);
  for (let page = 2; page <= pageCount; page += 1) {
    const nextPage = await listAvailableModels({ status: "active", page, page_size: pageSize });
    models.push(...nextPage.items);
  }
  return models;
}

export function PlaygroundScene({ id }: { id?: string }) {
  const { t } = useLanguage();
  const [application, setApplication] = useState<ApplicationDetail | null>(null);
  const [comparisonModels, setComparisonModels] = useState<ModelSummary[]>([]);
  const [request, setRequest] = useState<GatewayRequestDetail | null>(null);
  const [prompt, setPrompt] = useState("");
  const [comparison, setComparison] = useState<ComparisonRun | null>(null);
  const [qualityEvaluation, setQualityEvaluation] = useState(false);
  const [evaluatorModelId, setEvaluatorModelId] = useState("");
  const [executionStrategy, setExecutionStrategy] = useState<"sequential" | "parallel">("sequential");
  const [referenceModelIds, setReferenceModelIds] = useState<string[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "running" | "success" | "error">("loading");
  const [runningAction, setRunningAction] = useState<"routing" | "comparison">("routing");
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const loadedGatewayId = useRef<string | null>(null);
  const pendingComparison = useRef<{ fingerprint: string; key: string } | null>(null);
  const activeComparisonId = comparison && !comparisonIsTerminal(comparison.status) ? comparison.id : null;
  const comparisonPagination = useClientTablePagination(comparison?.branches ?? [], comparison?.id ?? null);

  useEffect(() => {
    if (!id) return;
    let active = true;
    const rememberedRequestId = window.sessionStorage.getItem(`gateway:last-request:${id}`);
    const rememberedComparisonId = window.sessionStorage.getItem(`gateway:last-comparison:${id}`);
    void Promise.all([
      getApplication(id),
      rememberedRequestId ? getGatewayRequest(rememberedRequestId) : Promise.resolve(null),
      listAllActiveModels().catch(() => []),
      rememberedComparisonId ? getGatewayComparison(rememberedComparisonId).catch(() => null) : Promise.resolve(null),
    ]).then(async ([value, previous, catalog, previousComparison]) => {
      // A historical comparison must not replace a more recent routing receipt.
      const receipt = previous ?? (previousComparison?.gateway_request_id
        ? await getGatewayRequest(previousComparison.gateway_request_id).catch(() => null)
        : null);
      const restoredComparison = previousComparison && (!receipt
        || previousComparison.gateway_request_id === receipt.request_id) ? previousComparison : null;
      if (!active) return;
      setApplication(value);
      setComparisonModels(catalog);
      setRequest(receipt);
      setPrompt(receipt?.prompt ?? "");
      loadedGatewayId.current = receipt?.request_id ?? null;
      setComparison(restoredComparison);
      if (restoredComparison) {
        setQualityEvaluation(restoredComparison.quality_evaluation);
        setEvaluatorModelId(restoredComparison.evaluator_model_id ?? "");
        setExecutionStrategy(restoredComparison.execution_strategy ?? "sequential");
        setReferenceModelIds(restoredComparison.branches
          .filter((branch) => branch.branch_type === "reference" && branch.model_id)
          .map((branch) => branch.model_id!));
        setPrompt(restoredComparison.prompt);
      }
      setRunningAction("comparison");
      setState(restoredComparison && !comparisonIsTerminal(restoredComparison.status) ? "running" : "ready");
      setError(null);
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("error");
      setError(messageFor(reason));
    });
    return () => {
      active = false;
      controllerRef.current?.abort();
    };
  }, [id]);

  useEffect(() => {
    if (!activeComparisonId || !id) return;
    let active = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const current = await getGatewayComparison(activeComparisonId!, { signal: controller.signal });
        if (!active) return;
        const gateway = current.branches.find((branch) => branch.branch_type === "gateway");
        if (current.gateway_request_id && ["completed", "partial"].includes(gateway?.status ?? "")
          && loadedGatewayId.current !== current.gateway_request_id) {
          try {
            const receipt = await getGatewayRequest(current.gateway_request_id);
            if (active) {
              setRequest(receipt);
              loadedGatewayId.current = current.gateway_request_id;
              window.sessionStorage.setItem(`gateway:last-request:${id}`, current.gateway_request_id);
            }
          } catch (reason) {
            if (active) setError(messageFor(reason));
          }
        }
        if (!active) return;
        setComparison(current);
        if (comparisonIsTerminal(current.status)) {
          setState(current.status === "failed" || current.status === "indeterminate" ? "error" : "success");
          setError(current.error_message ?? gateway?.error_message ?? null);
        }
        if (active && !comparisonIsTerminal(current.status)) timer = setTimeout(poll, 1500);
      } catch (reason) {
        if (!active) return;
        setError(messageFor(reason));
        timer = setTimeout(poll, 3000);
      }
    }
    void poll();
    return () => {
      active = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [activeComparisonId, id]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!id || !prompt.trim()) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setRunningAction("routing");
    setState("running");
    setError(null);
    setComparison(null);
    pendingComparison.current = null;
    try {
      window.sessionStorage.removeItem(`gateway:last-comparison:${id}`);
      const response = await createGatewayRequest(
        buildPlaygroundGatewayRequest(id, prompt),
        { signal: controller.signal },
      );
      const detail = await getGatewayRequest(response.request_id);
      if (controller.signal.aborted) return;
      setRequest(detail);
      loadedGatewayId.current = detail.request_id;
      window.sessionStorage.setItem(`gateway:last-request:${id}`, response.request_id);
      setState("success");
      await Promise.allSettled([
        getOverview({ application_id: id }),
        getUsage({ application_id: id, source: "playground" }),
      ]);
    } catch (reason) {
      if (controller.signal.aborted) return;
      setState("error");
      setError(messageFor(reason));
    }
  }

  async function startComparison() {
    if (!id || !request || !canCompareRouting(request, prompt) || referenceModelIds.length === 0
      || state === "running" || (qualityEvaluation && !evaluatorModelId)) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setRunningAction("comparison");
    setState("running");
    setError(null);
    const input = {
      application_id: id,
      gateway_request_id: request.request_id,
      prompt: request.prompt!,
      reference_model_ids: referenceModelIds,
      execution_strategy: executionStrategy,
      quality_evaluation: qualityEvaluation,
      evaluator_model_id: qualityEvaluation ? evaluatorModelId : null,
    };
    const fingerprint = JSON.stringify(input);
    if (pendingComparison.current?.fingerprint !== fingerprint) {
      pendingComparison.current = { fingerprint, key: crypto.randomUUID() };
    }
    try {
      const accepted = await createGatewayComparison({ ...input, idempotency_key: pendingComparison.current.key });
      window.sessionStorage.setItem(`gateway:last-comparison:${id}`, accepted.comparison_id);
      const current = await getGatewayComparison(accepted.comparison_id, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setComparison(current);
      pendingComparison.current = null;
      if (comparisonIsTerminal(current.status)) setState(current.status === "failed" || current.status === "indeterminate" ? "error" : "success");
    } catch (reason) {
      if (!controller.signal.aborted) { setError(messageFor(reason)); setState("error"); }
    }
  }

  if (state === "loading") return <PlaygroundSkeleton label={t("playground.loading")} />;
  if (state === "error" && !application) return <State message={error ?? t("playground.applicationLoadError")} error />;
  if (!application) return <State message={t("applications.notFound")} error />;
  const view = request ? toPlaygroundViewModel(request, (key, params) => t(key, params)) : null;
  // An offline pipeline has its own section and runs no model comparison (it calls no external model).
  const offline = application.pipeline === "offline";
  const base = offline ? `/offline/${application.id}` : `/applications/${application.id}`;

  return (
    <>
      <section className="gateway-page-heading">
        <div>
          <p className="gateway-eyebrow">{application.name} · Playground</p>
          <h1>{t("playground.title")}</h1>
          <p>{t("playground.description")}</p>
        </div>
        <Link href={base} className="gateway-button gateway-button--ghost">{offline ? "Pipeline overview" : t("playground.applicationOverview")}</Link>
      </section>
      <nav className="application-tabs" aria-label={t("applications.sections")}>
        <Link href={base}>{t("nav.overview")}</Link>
        <Link className="is-active" href={`${base}/playground`}>{t("playground.label")}</Link>
        <Link href={`${base}/settings`}>{t("applications.settings")}</Link>
      </nav>
      {error && <State message={error} error />}
      {comparison && <State
        message={comparisonProgress(comparison, (key) => t(key))}
        error={comparison.status === "failed" || comparison.status === "indeterminate" || comparison.evaluation_status === "failed"}
      />}
      <div className="playground-grid">
        <section className="gateway-panel composer">
          <div className="gateway-section-heading">
            <div><p className="gateway-eyebrow">{t("playground.promptLabel")}</p><h2>{t("playground.requestComposer")}</h2></div>
            <span className="gateway-status gateway-status--neutral"><span className="status-pin" aria-hidden="true" />{t("playground.liveBillable")}</span>
          </div>
          <form onSubmit={submit}>
            <label className="field">
              <span>{t("playground.promptRequired")}</span>
              <textarea rows={11} value={prompt} disabled={state === "running"} onChange={(event) => setPrompt(event.target.value)} placeholder={t("playground.promptPlaceholder")} />
              <small>{t("playground.estimatedTokens", { count: Math.ceil(prompt.length / 4) })}</small>
            </label>
            <div className="form-actions">
              <button type="button" className="gateway-button gateway-button--ghost" disabled={state === "running"} onClick={() => { setPrompt(""); setRequest(null); setComparison(null); window.sessionStorage.removeItem(`gateway:last-comparison:${id}`); window.sessionStorage.removeItem(`gateway:last-request:${id}`); setState("ready"); }}>{t("playground.clear")}</button>
              <button type="submit" className="gateway-button" disabled={!prompt.trim() || state === "running"}>{state === "running" && runningAction === "routing" ? t("playground.runningRouting") : t("playground.runRouting")}</button>
            </div>
          </form>
          {view && <dl className="request-metrics composer-metrics">
            <div><dt>{t("playground.selectedModel")}</dt><dd>{view.model}</dd></div>
            <div><dt>{t("usage.provider")}</dt><dd>{view.provider}</dd></div>
            <div><dt>{t("usage.latency")}</dt><dd>{view.latency}</dd></div>
            <div><dt>{t("usage.tokens")}</dt><dd>{view.tokens}</dd></div>
            <div><dt>{t("playground.estimatedCost")}</dt><dd>{view.cost}</dd></div>
            <div><dt>{t("reports.requestId")}</dt><dd>{view.requestId}</dd></div>
          </dl>}
        </section>
        <section className="gateway-panel response-panel" aria-live="polite">
          <div className="gateway-section-heading">
            <div><p className="gateway-eyebrow">{t("playground.routingResponse")}</p><h2>{view ? t("playground.savedRoutingResult") : state === "running" ? t("playground.runningPipeline") : t("playground.readyForTest")}</h2></div>
            {view && <span className="gateway-status gateway-status--healthy"><span className="status-pin" aria-hidden="true" />{view.status}</span>}
          </div>
          {!view && state !== "running" && <div className="response-empty">{t("playground.responseHere")}</div>}
          {state === "running" && !view && <div className="skeleton-stack"><i /><i /><i /></div>}
          {view && <>
            <MarkdownRenderer source={view.output} />
            {view.tasks.length > 1 && <section className="playground-task-routing" aria-label={t("playground.taskRoutingCost")}>
              <h3>{t("playground.taskRoutingCost")}</h3>
              <div>{view.tasks.map((task) => <article key={task.taskId}>
                <strong>{task.label}</strong>
                <span>{task.provider} · {task.model}</span>
                <small>{task.status} · {task.tokens} · {task.cost} · {task.latency}</small>
                {task.error && task.fallback && <small className="playground-task-fallback">{t("playground.fallbackUsed")}: {task.error}</small>}
                {task.error && !task.fallback && <small className="playground-task-error">{task.error}</small>}
              </article>)}</div>
            </section>}
            <p className="detail-copy">{view.fallback}</p>
          </>}
        </section>
      </div>
      {!offline && <PlaygroundCompareControls
        models={comparisonModels} request={request} prompt={prompt}
        referenceIds={referenceModelIds} evaluatorId={evaluatorModelId} qualityEvaluation={qualityEvaluation}
        strategy={executionStrategy} busy={state === "running"} comparing={state === "running" && runningAction === "comparison"}
        onReferencesChange={setReferenceModelIds} onEvaluatorChange={setEvaluatorModelId}
        onQualityChange={setQualityEvaluation} onStrategyChange={setExecutionStrategy}
        onCompare={() => void startComparison()}
      />}
      {comparison && <section className="gateway-panel comparison-report" aria-live="polite">
        <div className="gateway-section-heading">
          <div><p className="gateway-eyebrow">{t("playground.directComparison")}</p><h2>{t("playground.gatewayVersusModels")}</h2></div>
          <span className={`gateway-status ${comparison.status === "completed" ? "gateway-status--healthy" : "gateway-status--neutral"}`}>{comparison.status}</span>
        </div>
        <p className="detail-copy">{t("playground.experimentSpend", { amount: comparison.comparison_spend_usd.toFixed(6), evaluation: comparison.quality_evaluation ? t("playground.includingEvaluation", { amount: comparison.evaluation_cost_usd.toFixed(6) }) : "" })}</p>
        <p className="detail-copy">{t("playground.comparisonSummary", { strategy: comparison.execution_strategy ?? "sequential", quality: comparison.quality_evaluation ? comparison.evaluation_status ?? "pending" : t("playground.notRequested") })}</p>
        {comparison.quality_evaluation && <section className="comparison-evaluator" aria-label={t("playground.qualityEvaluation")}>
          <h3>{t("playground.evaluatorScoresAnswers")}</h3>
          <p className="detail-copy">{comparisonModels.find((model) => model.id === comparison.evaluator_model_id)?.display_name ?? comparison.evaluator_model_name ?? comparison.evaluator_model_id} · {comparison.evaluation_status ?? "pending"} · {comparison.evaluation_total_tokens ?? 0} tokens · USD ${comparison.evaluation_cost_usd.toFixed(6)}</p>
          {comparison.evaluation_error_message && <State message={`${comparison.evaluation_error_message}${comparison.evaluation_error_code ? ` (${comparison.evaluation_error_code})` : ""} ${t("playground.qualityUnavailable")}`} error />}
        </section>}
        <div className="gateway-table-wrap"><table className="gateway-table"><thead><tr><th>{t("playground.branch")}</th><th>{t("usage.status")}</th><th>{t("usage.tokens")}</th><th>{t("playground.observedCost")}</th><th>{t("usage.latency")}</th><th>{t("playground.quality")}</th></tr></thead><tbody>
          {comparisonPagination.pageItems.map((branch) => <tr key={branch.id}><td><strong>{branch.branch_type === "gateway" ? t("playground.gatewayRouting") : branch.model_name ?? branch.model_id}</strong><small>{branch.branch_type === "gateway" ? t("playground.allRoutedCalls") : t("playground.alternativeOriginal")}</small></td><td>{branch.status}{branch.error_message && <small>{branch.error_message}</small>}</td><td>{branch.total_tokens}</td><td>${branch.cost_usd.toFixed(6)}<small>{branch.usage_complete ? (branch.pricing_estimated ? t("playground.catalogPricing") : t("playground.providerReceipt")) : t("playground.incompleteUsage")}</small></td><td>{branch.latency_ms == null ? "—" : `${Math.round(branch.latency_ms)}ms`}</td><td>{branch.quality_score == null ? (comparison.evaluation_status === "failed" ? t("playground.notScored") : comparison.quality_evaluation ? comparison.evaluation_status ?? "pending" : t("playground.notRequested")) : branch.quality_score.toFixed(1)}</td></tr>)}
        </tbody></table></div>
        <PaginationControls page={comparisonPagination.page} pageSize={TABLE_PAGE_SIZE} total={comparison.branches.length} onPageChange={comparisonPagination.setPage} label={t("playground.comparisonPagination")} />
        {comparison.deltas.length > 0 && <div className="comparison-deltas">{comparison.deltas.map((delta) => {
          const branch = comparison.branches.find((item) => item.id === delta.reference_branch_id);
          return <article key={delta.reference_branch_id}><strong>{t("playground.comparedWith", { name: branch?.model_name ?? delta.reference_model_id })}</strong><span>{t("usage.cost")}: {delta.comparable_cost && delta.cost_delta_percent != null ? t("playground.percentSaved", { value: `${delta.cost_delta_percent >= 0 ? "+" : ""}${delta.cost_delta_percent.toFixed(1)}` }) : "N/A"}</span><span>{t("usage.latency")}: {delta.comparable_latency && delta.latency_delta_percent != null ? t("playground.percentFaster", { value: `${delta.latency_delta_percent >= 0 ? "+" : ""}${delta.latency_delta_percent.toFixed(1)}` }) : "N/A"}</span><span>{t("playground.quality")}: {delta.comparable_quality && delta.quality_delta_points != null ? t("playground.qualityPoints", { value: `${delta.quality_delta_points >= 0 ? "+" : ""}${delta.quality_delta_points.toFixed(1)}` }) : "N/A"}</span></article>;
        })}</div>}
        <div className="comparison-outputs">{comparison.branches.filter((branch) => branch.output).map((branch) => <article key={branch.id}><h3>{branch.branch_type === "gateway" ? t("playground.gatewayOutput") : t("playground.modelOutput", { name: branch.model_name ?? branch.model_id ?? "" })}</h3><MarkdownRenderer source={branch.output ?? ""} /></article>)}</div>
      </section>}
      {view && <section className="gateway-panel gateway-trace">
        <div className="gateway-section-heading">
          <div><p className="gateway-eyebrow">{t("playground.persistedTrace")}</p><h2>{t("playground.gatewayStages")}</h2></div>
          <span>{t("playground.stageCount", { count: view.traces.length })}</span>
        </div>
        <div className="trace-lineup">
          {view.traces.map((trace, index) => <article className={trace.status === "success" ? "is-complete" : ""} key={`${trace.stage}-${index}`}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <strong>{trace.stage}</strong>
            <small>{trace.strategy} · {trace.status} · {trace.latency}</small>
            {trace.error && <small>{trace.error}</small>}
          </article>)}
        </div>
      </section>}
    </>
  );
}
