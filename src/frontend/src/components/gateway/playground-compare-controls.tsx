"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useLanguage } from "@/components/language-provider";
import type { GatewayRequestDetail, ModelSummary } from "@/lib/gateway-types";
import { canCompareRouting } from "./playground-comparison";

function ModelSelector({ models, selectedIds, label, multiple = false, disabled, onSelect }: {
  models: ModelSummary[];
  selectedIds: string[];
  label: string;
  multiple?: boolean;
  disabled: boolean;
  onSelect: (id: string) => void;
}) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const panelId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!open || disabled) return;
    searchRef.current?.focus();
    const outside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setOpen(false); triggerRef.current?.focus(); }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open, disabled]);
  const search = query.trim().toLowerCase();
  const results = models.filter((model) => `${model.display_name} ${model.name} ${model.provider}`.toLowerCase().includes(search));
  const selected = models.find((model) => model.id === selectedIds[0]);
  return <div className="comparison-picker" ref={rootRef}>
    <button type="button" className="comparison-picker-trigger" ref={triggerRef} disabled={disabled || !models.length}
      aria-label={label} aria-expanded={open && !disabled} aria-controls={open && !disabled ? panelId : undefined}
      onClick={() => { setOpen((value) => !value); setQuery(""); }}>
      <span>{multiple ? (selectedIds.length ? t("playground.changeModels") : t("playground.chooseComparisonModels")) : selected?.display_name ?? t("playground.chooseEvaluator")}</span>
      <span aria-hidden="true">{open ? "−" : "+"}</span>
    </button>
    {open && !disabled && <div className="comparison-picker-menu" id={panelId}>
      <input ref={searchRef} type="search" aria-label={t("playground.searchModels")} placeholder={t("playground.searchModelPlaceholder")}
        value={query} onChange={(event) => setQuery(event.target.value)} autoComplete="off" />
      <div className="comparison-picker-results" role="group" aria-label={t("playground.availableModels")}>
        {results.map((model) => {
          const checked = selectedIds.includes(model.id);
          return <button type="button" key={model.id} aria-pressed={checked}
            disabled={multiple && !checked && selectedIds.length >= 3}
            onClick={() => {
              onSelect(model.id);
              if (!multiple) { setOpen(false); triggerRef.current?.focus(); }
            }}>
            <span className="comparison-picker-check" aria-hidden="true">{checked ? "✓" : ""}</span>
            <span><strong>{model.display_name ?? model.name}</strong><small>{model.provider}</small></span>
          </button>;
        })}
        {!results.length && <p>{t("playground.noMatchingModels")}</p>}
      </div>
      <div className="comparison-picker-bottom"><small>{multiple ? t("playground.selectedOfThree", { count: selectedIds.length }) : t("playground.selectOneEvaluator")}</small><button type="button" onClick={() => { setOpen(false); triggerRef.current?.focus(); }}>{t("playground.done")}</button></div>
    </div>}
  </div>;
}

export function PlaygroundCompareControls({ models, request, prompt, referenceIds, evaluatorId, qualityEvaluation,
  strategy, busy, comparing, onReferencesChange, onEvaluatorChange, onQualityChange, onStrategyChange, onCompare }: {
  models: ModelSummary[];
  request: GatewayRequestDetail | null;
  prompt: string;
  referenceIds: string[];
  evaluatorId: string;
  qualityEvaluation: boolean;
  strategy: "sequential" | "parallel";
  busy: boolean;
  comparing: boolean;
  onReferencesChange: (ids: string[]) => void;
  onEvaluatorChange: (id: string) => void;
  onQualityChange: (enabled: boolean) => void;
  onStrategyChange: (strategy: "sequential" | "parallel") => void;
  onCompare: () => void;
}) {
  const { t } = useLanguage();
  const routingReady = canCompareRouting(request, prompt);
  const modelById = new Map(models.map((model) => [model.id, model]));
  const selectedModelsReady = referenceIds.length > 0 && referenceIds.every((id) => modelById.has(id));
  const evaluatorReady = !qualityEvaluation || modelById.has(evaluatorId);
  const ready = routingReady && selectedModelsReady && evaluatorReady;
  const hint = !routingReady ? (request
    ? request.prompt !== prompt.trim() ? t("playground.promptChanged") : t("playground.routingIncomplete")
    : t("playground.runRoutingFirst"))
    : !selectedModelsReady ? t("playground.chooseAtLeastOne")
    : !evaluatorReady ? t("playground.chooseEvaluatorHint")
    : t("playground.additionalCalls", { count: referenceIds.length + (qualityEvaluation ? 1 : 0) });
  return <section className="gateway-panel comparison-setup" aria-label={t("playground.compareAfterRouting")}>
    <div className="gateway-section-heading">
      <div><p className="gateway-eyebrow">{t("playground.compareAfterRouting")}</p><h2>{t("playground.buildComparison")}</h2><p className="detail-copy">{t("playground.comparisonDescription")}</p></div>
      <span className={`gateway-status gateway-status--${routingReady ? "healthy" : "neutral"}`}>{routingReady ? t("playground.routingSaved") : t("playground.routingRequired")}</span>
    </div>
    <div className="comparison-setup-steps">
      <section className={`comparison-setup-step${routingReady ? " is-ready" : ""}`} aria-label={t("playground.savedRouting")}>
        <div className="comparison-step-heading"><span>{routingReady ? "✓" : "1"}</span><h3>{t("playground.savedRouting")}</h3><small>{t("playground.reused")}</small></div>
        <p>{t("playground.savedRoutingDescription")}</p>
        {request ? <div className="comparison-routing-receipt"><strong>{request.model ?? t("playground.applicationRouting")}</strong><small>{request.total_tokens ?? 0} tokens · USD ${(request.estimated_cost ?? 0).toFixed(6)}</small><code title={request.request_id}>{request.request_id}</code></div>
          : <div className="comparison-step-empty">{t("playground.routingResultHere")}</div>}
      </section>
      <section className="comparison-setup-step" aria-label={t("playground.comparisonModels")}>
        <div className="comparison-step-heading"><span>2</span><h3>{t("playground.alternativeAnswers")}</h3><small>{referenceIds.length}/3</small></div>
        <p>{t("playground.alternativeDescription")}</p>
        <ModelSelector models={models} selectedIds={referenceIds} label={t("playground.comparisonModels")} multiple disabled={busy}
          onSelect={(modelId) => onReferencesChange(referenceIds.includes(modelId) ? referenceIds.filter((value) => value !== modelId) : referenceIds.length < 3 ? [...referenceIds, modelId] : referenceIds)} />
        <div className="comparison-selected-models">
          {referenceIds.map((modelId) => <div className="comparison-model-chip" key={modelId}><span>{modelById.get(modelId)?.display_name ?? modelId}</span><button type="button" disabled={busy} aria-label={t("playground.removeModel", { name: modelById.get(modelId)?.display_name ?? modelId })} onClick={() => onReferencesChange(referenceIds.filter((value) => value !== modelId))}>×</button></div>)}
          {!referenceIds.length && <small>{t("playground.noModelsSelected")}</small>}
        </div>
      </section>
      <section className="comparison-setup-step" aria-label={t("playground.qualityScoring")}>
        <div className="comparison-step-heading"><span>3</span><h3>{t("playground.qualityScoring")}</h3><small>{t("playground.optional")}</small></div>
        <p>{t("playground.evaluatorDescription")}</p>
        <label className="comparison-quality-toggle"><input type="checkbox" disabled={busy} checked={qualityEvaluation} onChange={(event) => onQualityChange(event.target.checked)} /><span>{t("playground.includeQuality")}</span></label>
        {qualityEvaluation ? <>
          <ModelSelector models={models} selectedIds={evaluatorId ? [evaluatorId] : []} label={t("playground.qualityEvaluator")} disabled={busy} onSelect={onEvaluatorChange} />
          {referenceIds.includes(evaluatorId) && <small className="comparison-role-note">{t("playground.evaluatorAlsoAnswers")}</small>}
        </> : <div className="comparison-step-empty">{t("playground.costLatencyOnly")}</div>}
      </section>
    </div>
    <div className="comparison-setup-footer">
      <div className="comparison-execution-control"><span>{t("playground.runAlternatives")}</span><div role="group" aria-label={t("playground.comparisonExecution")}>
        <button type="button" aria-pressed={strategy === "sequential"} disabled={busy} onClick={() => onStrategyChange("sequential")}>{t("playground.sequential")}</button>
        <button type="button" aria-pressed={strategy === "parallel"} disabled={busy} onClick={() => onStrategyChange("parallel")}>{t("playground.parallel")}</button>
      </div></div>
      <div className="comparison-start"><small role="status">{hint}</small><button type="button" className="gateway-button" disabled={busy || !ready} onClick={onCompare}>{comparing ? t("playground.comparing") : t("playground.runComparison")}<span aria-hidden="true"> →</span></button></div>
    </div>
    {!models.length && <p className="detail-copy">{t("playground.noCallableModels")}</p>}
  </section>;
}
