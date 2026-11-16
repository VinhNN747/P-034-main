"use client";

import { useEffect, useRef, useState } from "react";
import type { Chart } from "highcharts";
import type { SupportData } from "@/components/support-analytics";
import { useLanguage } from "@/components/language-provider";
import { supportChartOptions } from "@/lib/support-chart-options";

let runtime: Promise<typeof import("highcharts")> | null = null;
function loadHighcharts() {
  if (!runtime) runtime = (async () => {
    const highcharts = await import("highcharts");
    await import("highcharts/modules/exporting");
    await import("highcharts/modules/accessibility");
    return highcharts;
  })().catch((cause) => { runtime = null; throw cause; });
  return runtime;
}

export function SupportHighchart({ data, type }: { data: SupportData; type: "line" | "bar" }) {
  const { t } = useLanguage();
  const previewRef = useRef<HTMLDivElement>(null);
  const modalRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const previewChart = useRef<Chart | null>(null);
  const modalChart = useRef<Chart | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [ready, setReady] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let chart: Chart | null = null;
    let resizeObserver: ResizeObserver | null = null;
    void loadHighcharts().then((Highcharts) => {
      if (!active || !previewRef.current) return;
      chart = Highcharts.chart(previewRef.current, supportChartOptions(data, type, false, () => setIsOpen(true)));
      previewChart.current = chart;
      if (typeof ResizeObserver !== "undefined") {
        resizeObserver = new ResizeObserver(() => chart?.reflow());
        resizeObserver.observe(previewRef.current);
      }
      setReady(true);
      setError(null);
    }).catch(() => { if (active) setError(t("charts.loadError")); });
    return () => { active = false; resizeObserver?.disconnect(); if (previewChart.current === chart) previewChart.current = null; chart?.destroy(); };
  }, [data, type, t]);

  useEffect(() => {
    if (!isOpen || !dialogRef.current) return;
    let active = true;
    let chart: Chart | null = null;
    const dialog = dialogRef.current;
    const previousOverflow = document.body.style.overflow;
    dialog.showModal();
    document.body.style.overflow = "hidden";
    void loadHighcharts().then((Highcharts) => {
      if (!active || !modalRef.current) return;
      chart = Highcharts.chart(modalRef.current, supportChartOptions(data, type, true));
      modalChart.current = chart;
    }).catch(() => { if (active) setError(t("charts.openError")); });
    return () => {
      active = false;
      if (modalChart.current === chart) modalChart.current = null;
      chart?.destroy();
      dialog.close();
      document.body.style.overflow = previousOverflow;
    };
  }, [isOpen, data, type, t]);

  async function download() {
    const chart = modalChart.current ?? previewChart.current;
    if (!chart || exporting) return;
    setExporting(true);
    setError(null);
    try {
      await chart.exporting.exportChart({ type: "image/png", local: true, fallbackToExportServer: false });
    } catch {
      setError(t("charts.exportError"));
    } finally {
      setExporting(false);
    }
  }

  return <div className="support-highchart">
    <div className="support-analytics-controls">
      <button type="button" aria-haspopup="dialog" onClick={() => setIsOpen(true)} disabled={!ready}>{t("charts.expand")}</button>
      <button type="button" onClick={() => void download()} disabled={!ready || exporting}>{exporting ? t("charts.exporting") : t("charts.downloadPng")}</button>
    </div>
    {!ready && !error && <small role="status">{t("charts.rendering")}</small>}
    <div ref={previewRef} className="support-highchart-canvas" />
    <small className="support-chart-preview-hint">{t("charts.previewHint")}</small>
    {error && <p role="alert">{error}</p>}
    <dialog
      ref={dialogRef} className="support-chart-lightbox" aria-label={data.title}
      onKeyDown={(event) => { if (event.key === "Escape") event.stopPropagation(); }}
      onCancel={(event) => { event.preventDefault(); setIsOpen(false); }}
      onClose={() => setIsOpen(false)}
      onClick={(event) => { if (event.target === event.currentTarget) setIsOpen(false); }}
    >
      <div className="support-chart-lightbox-panel">
        <header className="support-chart-lightbox-header">
          <h2>{data.title}</h2>
          <div className="support-chart-lightbox-actions">
            <button type="button" aria-label={t("charts.close")} onClick={() => setIsOpen(false)}>×</button>
            <button type="button" className="support-chart-download" onClick={() => void download()} disabled={exporting}>{exporting ? t("charts.exporting") : t("charts.downloadPng")}</button>
          </div>
        </header>
        {error && <p role="alert">{error}</p>}
        <div ref={modalRef} className="support-highchart-expanded" />
      </div>
    </dialog>
  </div>;
}
