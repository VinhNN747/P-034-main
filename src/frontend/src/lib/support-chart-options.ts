import type { Options, SeriesOptionsType } from "highcharts";
import type { SupportData } from "@/components/support-analytics";
import { formatSupportNumber, supportDecimalPlaces } from "@/lib/support-number-format";

export function chartText(value: string): string {
  return value.replace("zoomed Y-axis", "trục Y được thu phóng / zoomed Y-axis").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]!));
}

export function supportChartOptions(data: SupportData, type: "line" | "bar", expanded = false, onExpand?: () => void): Options {
  const seriesType = type === "line" && data.operation !== "breakdown" ? "line" : "column";
  const values = data.rows.map((row) => row.value);
  const numericValues = values.filter((value): value is number => value !== null && Number.isFinite(value));
  const decimals = supportDecimalPlaces(values, data.unit);
  const minimum = numericValues.length ? Math.min(...numericValues) : 0;
  const maximum = numericValues.length ? Math.max(...numericValues) : 0;
  const range = maximum - minimum;
  const focusLine = seriesType === "line" && numericValues.length > 1 && range > 0;
  const padding = focusLine ? range * 0.12 : 0;
  const axisMinimum = focusLine ? (minimum >= 0 ? Math.max(0, minimum - padding) : minimum - padding) : minimum < 0 ? undefined : 0;
  const axisMaximum = focusLine ? maximum + padding : undefined;
  const zoomedFromZero = focusLine && minimum > 0 && axisMinimum !== 0;
  const requestSeries = data.dimension === "request";
  const format = (value: number) => formatSupportNumber(value, values, data.unit);
  return {
    chart: {
      type: seriesType, animation: false, backgroundColor: "#ffffff", reflow: true,
      style: { fontFamily: "Arial, sans-serif" },
      numberFormatter: (value) => format(value),
      events: onExpand ? { click: onExpand } : undefined,
    },
    title: { text: expanded ? chartText(data.title) : "", style: { fontSize: "18px" } },
    subtitle: { text: expanded ? chartText(data.unit + " · " + data.timezone + (zoomedFromZero ? " · zoomed Y-axis" : "")) : "" },
    xAxis: {
      categories: data.rows.map((row, index) => requestSeries ? `#${index + 1}` : chartText(row.label)),
      title: { text: requestSeries ? "Thứ tự request" : undefined },
      labels: {
        step: requestSeries && data.rows.length > (expanded ? 24 : 10) ? Math.ceil(data.rows.length / (expanded ? 24 : 10)) : 1,
        style: { fontSize: expanded ? "12px" : "10px" },
      },
    },
    yAxis: {
      title: { text: chartText(data.unit + (zoomedFromZero ? " (zoomed)" : "")) },
      min: axisMinimum,
      max: axisMaximum,
      startOnTick: !focusLine,
      endOnTick: !focusLine,
      tickAmount: focusLine ? (expanded ? 6 : 5) : undefined,
      labels: { formatter() { return typeof this.value === "number" ? format(this.value) : String(this.value); } },
    },
    legend: { enabled: false },
    tooltip: {
      useHTML: false,
      formatter() {
        const row = data.rows[this.index];
        const value = typeof this.y === "number" ? this.y : null;
        if (!row || value === null) return false;
        let result = requestSeries
          ? `<b>Request #${this.index + 1}</b><br/>ID: ${chartText(row.label)}<br/>${chartText(data.title)}: <b>${format(value)} ${chartText(data.unit)}</b>`
          : `<b>${chartText(row.label)}</b><br/>${chartText(data.title)}: <b>${format(value)} ${chartText(data.unit)}</b>`;
        if (row.detail) result += `<br/>${chartText(row.detail)}`;
        const previous = this.index > 0 ? data.rows[this.index - 1]?.value : null;
        if (typeof previous === "number" && Number.isFinite(previous)) {
          const delta = value - previous;
          const sign = delta > 0 ? "+" : "";
          const percent = previous === 0 ? "" : ` (${sign}${new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 }).format(delta / previous * 100)}%)`;
          result += `<br/>Chênh lệch: <b>${sign}${format(delta)} ${chartText(data.unit)}${percent}</b>`;
        }
        return result;
      },
      valueDecimals: decimals,
    },
    plotOptions: { series: { animation: false, connectNulls: false, cursor: onExpand ? "zoom-in" : "default", events: onExpand ? { click: onExpand } : undefined } },
    series: [{ type: seriesType, name: chartText(data.title), color: "#2563eb", data: data.rows.map((row) => row.value) } as SeriesOptionsType],
    accessibility: { enabled: true, description: chartText(data.metric_definition) },
    exporting: {
      enabled: false, local: true, fallbackToExportServer: false, filename: "usage-chart",
      sourceWidth: 1200, sourceHeight: 720,
      chartOptions: { chart: { style: { fontFamily: "Arial, sans-serif" } }, title: { text: chartText(data.title) } },
    },
  };
}
