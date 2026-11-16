import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { SupportAnalytics, type SupportData } from "@/components/support-analytics";

function dataset(update: Partial<SupportData> = {}): SupportData {
  return {
    id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", operation: "timeseries", metric: "cost_usd",
    dimension: "day", title: "Chi phí", unit: "USD", timezone: "UTC+7", as_of: "2026-10-08",
    metric_definition: "Chi phí execution", rows: [{ label: "2026-10-07", value: 0.25, requests: 1 }],
    chart_type: null, truncated: false, ...update,
  };
}

describe("support analytics presentation", () => {
  it("shows a data table and chart controls without requiring a chart", () => {
    const html = renderToStaticMarkup(<SupportAnalytics data={dataset()} />);
    expect(html).toContain("0,25");
    expect(html).toContain("Biểu đồ đường");
    expect(html).toContain("Biểu đồ cột");
    expect(html).not.toContain("<img");
  });
  it("does not offer line charts for categorical breakdowns", () => {
    const html = renderToStaticMarkup(<SupportAnalytics data={dataset({ operation: "breakdown", dimension: "model" })} />);
    expect(html).not.toContain("Biểu đồ đường");
    expect(html).toContain("Biểu đồ cột");
  });
  it("renders missing values and failures without misleading chart controls", () => {
    const html = renderToStaticMarkup(<SupportAnalytics data={dataset({
      operation: "failures", rows: [{ label: "<script>secret</script>", value: null, requests: 1 }],
    })} />);
    expect(html).toContain("N/A");
    expect(html).not.toContain("Biểu đồ đường");
    expect(html).not.toContain("<script>");
  });
  it("does not round tiny USD differences out of the data table", () => {
    const html = renderToStaticMarkup(<SupportAnalytics data={dataset({
      rows: [
        { label: "2026-10-07", value: 0.00001001, requests: 1 },
        { label: "2026-10-08", value: 0.00001002, requests: 1 },
      ],
    })} />);
    expect(html).toContain("0,00001001");
    expect(html).toContain("0,00001002");
  });
  it("shows request context in a separate comparison-table column", () => {
    const html = renderToStaticMarkup(<SupportAnalytics data={dataset({
      dimension: "request",
      rows: [{ label: "req-001", value: 0.12, requests: 1, detail: "2026-10-10T08:00:00Z · Alpha · 2 usage events" }],
    })} />);
    expect(html).toContain("Yêu cầu");
    expect(html).toContain("Thời điểm · application");
    expect(html).toContain("req-001");
    expect(html).toContain("Alpha");
  });
});
