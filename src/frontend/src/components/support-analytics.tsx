"use client";

import { useEffect, useState } from "react";

import { SupportHighchart } from "@/components/support-highchart";
import { useLanguage } from "@/components/language-provider";
import { PaginationControls, TABLE_PAGE_SIZE, useClientTablePagination } from "@/components/gateway/pagination-controls";
import { formatSupportNumber } from "@/lib/support-number-format";

export type SupportData = {
  id: string;
  operation: "timeseries" | "breakdown" | "compare" | "failures";
  metric: string;
  title: string;
  dimension: string;
  unit: string;
  timezone: string;
  as_of: string;
  metric_definition: string;
  rows: { label: string; value: number | null; requests: number; detail?: string | null }[];
  chart_type?: "line" | "bar" | null;
  truncated: boolean;
};

const validId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function SupportAnalytics({ data }: { data: SupportData }) {
  const { t } = useLanguage();
  const [chartType, setChartType] = useState<"line" | "bar" | null>(data.chart_type ?? null);
  const [snapshot, setSnapshot] = useState<SupportData | null>(null);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const tablePagination = useClientTablePagination(data.rows, data.id);
  const plottable = data.operation !== "failures" && data.rows.some((row) => row.value !== null);
  const enabled = !!chartType;
  useEffect(() => {
    if (!enabled || !plottable || !validId.test(data.id)) return;
    const controller = new AbortController();
    async function loadChart() {
      try {
        const response = await fetch(`/api/beta-chat/charts/${encodeURIComponent(data.id)}`, {
          cache: "no-store", signal: controller.signal,
        });
        const body = await response.json() as SupportData & { error?: string; detail?: string };
        if (!response.ok) throw new Error(body.error ?? body.detail ?? t("charts.loadError"));
        if (body.id !== data.id || !Array.isArray(body.rows)) throw new Error(t("charts.invalidData"));
        if (controller.signal.aborted) return;
        setSnapshot(body);
        setError(null);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : t("charts.loadError"));
      }
    }
    void loadChart();
    return () => controller.abort();
  }, [enabled, data.id, plottable, revision, t]);

  function chooseChart(type: "line" | "bar") { setChartType(type); }
  const currentData = snapshot?.id === data.id ? snapshot : null;
  const values = data.rows.map((row) => row.value);
  return <div className="support-analytics">
    {plottable && <div className="support-analytics-controls" aria-label={t("analytics.chartType")}>
      {data.operation !== "breakdown" && <button type="button" aria-pressed={chartType === "line"} onClick={() => chooseChart("line")}>{t("analytics.lineChart")}</button>}
      <button type="button" aria-pressed={chartType === "bar"} onClick={() => chooseChart("bar")}>{t("analytics.barChart")}</button>
    </div>}
    {chartType && plottable && !currentData && !error && <small role="status">{t("analytics.loadingChart")}</small>}
    {chartType && plottable && currentData && !error && <figure>
      <SupportHighchart data={currentData} type={chartType} />
      <figcaption>{data.unit} · {data.timezone}</figcaption>
    </figure>}
    {error && <p role="alert">{error} {t("analytics.reviewTable")} <button type="button" onClick={() => { setError(null); setSnapshot(null); setRevision((value) => value + 1); }}>{t("common.tryAgain")}</button></p>}
    <details open={!chartType || !!error}>
      <summary>{t("analytics.dataTable")}{data.truncated ? ` (${t("analytics.limited")})` : ""}</summary>
      <div className="support-analytics-table"><table className={data.dimension === "request" ? "support-request-comparison-table" : undefined}>
        <thead><tr><th>{data.operation === "failures" || data.dimension === "request" ? t("analytics.request") : t("analytics.group")}</th>{data.dimension === "request" && <th>{t("analytics.timeApplication")}</th>}<th>{data.operation === "failures" ? "ms" : data.unit}</th></tr></thead>
        <tbody>{tablePagination.pageItems.map((row, index) => <tr key={`${row.label}-${index}`}>
          <td>{data.dimension === "request" ? <span className="support-request-identity"><b>#{(tablePagination.page - 1) * TABLE_PAGE_SIZE + index + 1}</b><code title={row.label}>{row.label}</code></span> : <>{row.label}{row.detail && <small>{row.detail}</small>}</>}</td>
          {data.dimension === "request" && <td>{row.detail ?? "—"}</td>}
          <td>{row.value === null ? "N/A" : formatSupportNumber(row.value, values, data.unit)}</td>
        </tr>)}</tbody>
      </table></div>
      <PaginationControls page={tablePagination.page} pageSize={TABLE_PAGE_SIZE} total={data.rows.length} onPageChange={tablePagination.setPage} label={t("analytics.pagination")} />
      {!data.rows.length && <small>{t("analytics.noData")}</small>}
    </details>
    <small className="support-analytics-definition">{data.metric_definition}</small>
  </div>;
}
