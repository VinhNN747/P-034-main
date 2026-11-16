import { describe, expect, it } from "vitest";
import { supportChartOptions } from "@/lib/support-chart-options";
import type { SupportData } from "@/components/support-analytics";

const data: SupportData = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", operation: "timeseries", metric: "success_rate",
  title: "Success rate", dimension: "day", unit: "%", timezone: "UTC+7", as_of: "2026-10-08",
  metric_definition: "Completed requests / all requests",
  rows: [{ label: "Today", value: 0, requests: 1 }, { label: "Yesterday", value: null, requests: 0 }], truncated: false,
};

describe("Highcharts support data presentation", () => {
  it("keeps a measured zero and missing data distinct", () => {
    const options = supportChartOptions(data, "line");
    expect(options.series?.[0]).toMatchObject({ type: "line", data: [0, null] });
    expect(options.plotOptions?.series?.connectNulls).toBe(false);
  });
  it("uses columns for categorical groups even if line is requested", () => {
    expect(supportChartOptions({ ...data, operation: "breakdown" }, "line").series?.[0]).toMatchObject({ type: "column" });
  });
  it("escapes untrusted labels and prevents external export fallback", () => {
    const options = supportChartOptions({ ...data, title: '<img src=x onerror="alert(1)">', rows: [{ label: "<script>x</script>", value: 1, requests: 1 }] }, "bar", true);
    expect(options.title?.text).not.toContain("<img");
    expect(options.xAxis).toMatchObject({ categories: ["&lt;script&gt;x&lt;/script&gt;"] });
    expect(options.exporting).toMatchObject({ local: true, fallbackToExportServer: false });
  });
  it("zooms a line chart to reveal tiny differences and keeps enough USD precision", () => {
    const options = supportChartOptions({
      ...data,
      metric: "cost_usd",
      unit: "USD",
      rows: [
        { label: "2026-10-07", value: 0.10001, requests: 1 },
        { label: "2026-10-08", value: 0.10002, requests: 1 },
      ],
    }, "line", true);
    expect(options.yAxis).toMatchObject({ startOnTick: false, endOnTick: false });
    expect((options.yAxis as { min?: number }).min).toBeCloseTo(0.1000088);
    expect((options.yAxis as { max?: number }).max).toBeCloseTo(0.1000212);
    expect(options.subtitle?.text).toContain("trục Y được thu phóng");
    expect(options.chart?.numberFormatter?.(0.10001, -1, ",", ".")).toBe("0,10001");
    const tooltip = options.tooltip?.formatter?.call({ index: 1, y: 0.10002 } as never, {} as never);
    expect(tooltip).toContain("Chênh lệch: <b>+0,00001 USD (+0,01%)</b>");
  });
  it("keeps a zero baseline for columns", () => {
    const options = supportChartOptions({
      ...data,
      metric: "cost_usd",
      unit: "USD",
      rows: [{ label: "A", value: 0.10001, requests: 1 }, { label: "B", value: 0.10002, requests: 1 }],
    }, "bar");
    expect(options.yAxis).toMatchObject({ min: 0 });
  });
  it("uses compact request sequence labels while preserving request metadata in the tooltip", () => {
    const options = supportChartOptions({
      ...data,
      metric: "latency_ms",
      dimension: "request",
      unit: "ms",
      rows: [
        { label: "request-with-a-long-id-1", value: 120, requests: 1, detail: "2026-10-10T08:00:00Z · Alpha · completed" },
        { label: "request-with-a-long-id-2", value: 180, requests: 1, detail: "2026-10-10T08:01:00Z · Alpha · completed" },
      ],
    }, "line", true);
    expect(options.xAxis).toMatchObject({ categories: ["#1", "#2"], title: { text: "Thứ tự request" } });
    const tooltip = options.tooltip?.formatter?.call({ index: 1, y: 180 } as never, {} as never);
    expect(tooltip).toContain("Request #2");
    expect(tooltip).toContain("ID: request-with-a-long-id-2");
    expect(tooltip).toContain("Alpha");
    expect(tooltip).toContain("Chênh lệch: <b>+60 ms (+50%)</b>");
  });
});
