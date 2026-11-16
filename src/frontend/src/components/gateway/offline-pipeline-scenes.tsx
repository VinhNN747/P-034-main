"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { ApplicationKeyPanel } from "@/components/gateway/application-key-panel";
import { DetailSkeleton, FormPageSkeleton, ListPageSkeleton } from "@/components/gateway/gateway-page-skeletons";
import { useLanguage } from "@/components/language-provider";
import { getRouteViewState } from "@/lib/gateway-navigation";
import {
  createOfflinePipeline,
  deleteOfflinePipeline,
  gatewayOpenAIBaseUrl,
  getOfflinePipeline,
  listGatewayRequests,
  listMyEndpoints,
  listOfflinePipelines,
  updateOfflinePipeline,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type {
  CustomerEndpoint,
  EndpointQualityTier,
  GatewayRequestSummary,
  OfflineLadder,
  OfflinePipeline,
} from "@/lib/gateway-types";

type Translate = ReturnType<typeof useLanguage>["t"];

export const TIERS: EndpointQualityTier[] = ["simple", "mid", "hard"];

/** Presets measured with eval/task_type/calibrate_thresholds.py (validation split). */
export const THRESHOLD_PRESETS = [
  { key: "saver", mid: 0.25, large: 0.65 },
  { key: "balanced", mid: 0.23, large: 0.61 },
  { key: "quality", mid: 0.25, large: 0.55 },
] as const;
export type PresetKey = (typeof THRESHOLD_PRESETS)[number]["key"] | "custom";

export function presetFor(mid: number, large: number): PresetKey {
  const match = THRESHOLD_PRESETS.find((preset) => Math.abs(preset.mid - mid) < 1e-9 && Math.abs(preset.large - large) < 1e-9);
  return match?.key ?? "custom";
}

/** A URL-safe slug from a display name: "Chatbot nội bộ" -> "chatbot-noi-bo". */
export function slugify(name: string): string {
  return name
    .normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/đ/g, "d").replace(/Đ/g, "d")
    .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 100);
}

export type OfflineDraft = {
  name: string;
  slug: string;
  description: string;
  models: Partial<OfflineLadder>;
  mid: number;
  large: number;
  maxOutputTokens: number;
  timeoutSeconds: number;
  fallbackEnabled: boolean;
};

/** The first problem with a draft, as a translation key, or null when it can be sent. */
export function draftProblem(draft: OfflineDraft): "offline.errorName" | "offline.errorSlug" | "offline.errorModels" | "offline.errorThresholds" | null {
  if (!draft.name.trim()) return "offline.errorName";
  if (!/^[a-z0-9][a-z0-9-]*$/.test(draft.slug)) return "offline.errorSlug";
  const chosen = TIERS.map((tier) => draft.models[tier]).filter(Boolean);
  if (chosen.length !== 3 || new Set(chosen).size !== 3) return "offline.errorModels";
  const inRange = (value: number) => Number.isFinite(value) && value >= 0 && value <= 1;
  if (!inRange(draft.mid) || !inRange(draft.large) || draft.mid >= draft.large) return "offline.errorThresholds";
  return null;
}

/** Models a tier can use: probed (active) models of that tier, on any of the user's endpoints. */
export function tierOptions(endpoints: CustomerEndpoint[], tier: EndpointQualityTier) {
  return endpoints.flatMap((endpoint) => endpoint.models
    .filter((model) => model.status === "active" && model.quality_tier === tier)
    .map((model) => ({ id: model.id, label: `${model.name} · ${endpoint.display_name}`, health: model.health_status, context: model.context_window })));
}

export function coveredTiers(endpoints: CustomerEndpoint[]): number {
  return TIERS.filter((tier) => tierOptions(endpoints, tier).length > 0).length;
}

export function offlineErrorMessage(error: unknown, t: Translate): string {
  if (error && typeof error === "object" && "status" in error) {
    const apiError = error as GatewayApiError;
    if (apiError.status === 401 || apiError.status === 403) return t("offline.errorPermission");
    if (apiError.status === 503) return t("offline.errorUnavailable");
    if ([404, 409, 422].includes(apiError.status) && apiError.message) return apiError.message;
  }
  return t("offline.errorGeneric");
}

/** The endpoints a pipeline's ladder runs on, in ladder order, with the tiers each one serves. */
export function pipelineEndpoints(pipeline: OfflinePipeline): { id: string; name: string; tiers: EndpointQualityTier[] }[] {
  const byId = new Map<string, { id: string; name: string; tiers: EndpointQualityTier[] }>();
  for (const model of pipeline.models) {
    const entry = byId.get(model.endpoint_id) ?? { id: model.endpoint_id, name: model.endpoint_name, tiers: [] };
    entry.tiers.push(model.tier);
    byId.set(model.endpoint_id, entry);
  }
  return [...byId.values()];
}

const formatThreshold =(value: number) => value.toFixed(2).replace(".", ",");

function tierName(tier: EndpointQualityTier, t: Translate) {
  return t(tier === "simple" ? "offline.tierSimple" : tier === "mid" ? "offline.tierMid" : "offline.tierHard");
}

function presetLabel(key: PresetKey, t: Translate) {
  return t(key === "saver" ? "offline.presetSaver" : key === "balanced" ? "offline.presetBalanced" : key === "quality" ? "offline.presetQuality" : "offline.presetCustom");
}

function statusChip(status: OfflinePipeline["status"], t: Translate) {
  return <span className={"provider-connection-status provider-connection-status--" + (status === "active" ? "active" : "inactive")}>{status === "active" ? t("offline.statusActive") : t("offline.statusPaused")}</span>;
}

function ThresholdBar({ mid, large }: { mid: number; large: number }) {
  const parts = [mid, Math.max(0, large - mid), Math.max(0, 1 - large)].map((share) => `${(share * 100).toFixed(1)}%`);
  return <>
    <div className="threshold-bar" aria-hidden="true">{parts.map((basis, index) => <span key={index} style={{ flexBasis: basis }} />)}</div>
    <div className="threshold-scale" aria-hidden="true">
      <span style={{ flexBasis: parts[0] }}>Small &lt; {formatThreshold(mid)}</span>
      <span style={{ flexBasis: parts[1] }}>Mid {formatThreshold(mid)} – {formatThreshold(large)}</span>
      <span style={{ flexBasis: parts[2] }}>Large ≥ {formatThreshold(large)}</span>
    </div>
  </>;
}

// ---- /offline ---------------------------------------------------------------------------------------

/** Tabs shared by the Offline section's two pages; shown only when both are open to the user. */
export function OfflineSectionTabs({ active, permissions }: { active: "/offline" | "/my-endpoints"; permissions: readonly string[] }) {
  const { t } = useLanguage();
  if (getRouteViewState("/offline", permissions) === "restricted" || getRouteViewState("/my-endpoints", permissions) === "restricted") return null;
  return <nav className="application-tabs" aria-label={t("nav.offline")}>
    <Link className={active === "/offline" ? "is-active" : ""} aria-current={active === "/offline" ? "page" : undefined} href="/offline">{t("offline.tabPipelines")}</Link>
    <Link className={active === "/my-endpoints" ? "is-active" : ""} aria-current={active === "/my-endpoints" ? "page" : undefined} href="/my-endpoints">{t("nav.myEndpoints")}</Link>
  </nav>;
}

export function OfflinePipelinesScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const canManage = permissions.includes("applications.manage_own") && permissions.includes("model_pools.manage_own");
  const [pipelines, setPipelines] = useState<OfflinePipeline[]>([]);
  const [endpoints, setEndpoints] = useState<CustomerEndpoint[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  async function remove(pipeline: OfflinePipeline) {
    if (!window.confirm(t("offline.deleteConfirm", { name: pipeline.name }))) return;
    setDeletingId(pipeline.id); setError(null); setMessage(null);
    try {
      await deleteOfflinePipeline(pipeline.id);
      setPipelines((current) => current.filter((item) => item.id !== pipeline.id));
      setMessage(t("offline.deleted", { name: pipeline.name }));
    } catch (reason) {
      setError(offlineErrorMessage(reason, t));
    } finally {
      setDeletingId(null);
    }
  }

  useEffect(() => {
    let active = true;
    Promise.all([listOfflinePipelines(), permissions.includes("model_pools.read_own") ? listMyEndpoints() : Promise.resolve([])])
      .then(([items, mine]) => { if (!active) return; setPipelines(items); setEndpoints(mine); setState("ready"); })
      .catch((reason: unknown) => { if (!active) return; setError(offlineErrorMessage(reason, t)); setState("error"); });
    return () => { active = false; };
  }, [permissions, t]);

  if (state === "loading") return <ListPageSkeleton label={t("offline.title")} filters={0} columns={7} />;

  const activeCount = pipelines.filter((item) => item.status === "active").length;
  const activeModels = endpoints.reduce((total, endpoint) => total + endpoint.models.filter((model) => model.status === "active").length, 0);
  const tiers = coveredTiers(endpoints);
  const requests = pipelines.reduce((total, item) => total + item.request_count, 0);

  return <>
    <section className="gateway-page-heading">
      <div><p className="gateway-eyebrow">{t("offline.eyebrow")}</p><h1>{t("offline.title")}</h1><p>{t("offline.description")}</p></div>
      {canManage && <Link href="/offline/new" className="gateway-button">{t("offline.new")}</Link>}
    </section>
    <OfflineSectionTabs active="/offline" permissions={permissions} />

    <section className="gateway-policy-grid">
      <article><span>{t("offline.statPipelines")}</span><strong>{t("offline.statPipelinesValue", { count: pipelines.length })}</strong><small>{t("offline.statPipelinesDetail", { active: activeCount, paused: pipelines.length - activeCount })}</small></article>
      <article><span>{t("offline.statModels")}</span><strong>{t("offline.statModelsValue", { count: activeModels })}</strong><small>{t("offline.statModelsDetail", { endpoints: endpoints.length, tiers })}</small></article>
      <article><span>{t("offline.statRequests")}</span><strong>{requests.toLocaleString()}</strong><small>{t("offline.statRequestsDetail")}</small></article>
      <article><span>{t("offline.statTokens")}</span><strong>0</strong><small>{t("offline.statTokensDetail")}</small></article>
    </section>

    {error && <div className="notice notice--warning" role="alert">{error}</div>}
    {message && <div className="notice" role="status">{message}</div>}
    <div className="notice" role="note">{t("offline.privacy")}</div>
    {canManage && tiers < 3 && <div className="notice notice--warning" role="note">{t("offline.needTiers")} <Link href="/my-endpoints">{t("offline.addEndpoints")}</Link></div>}

    {state === "ready" && pipelines.length === 0 ? <section className="gateway-panel model-pool-empty">
      <h2>{t("offline.emptyTitle")}</h2><p>{t("offline.emptyDescription")}</p>
      {canManage && <Link href="/offline/new" className="gateway-button">{t("offline.new")}</Link>}
    </section> : <section className="gateway-panel table-panel">
      <div className="gateway-section-heading"><div><h2>{t("offline.listTitle")}</h2><p>{t("offline.listDescription")}</p></div></div>
      <div className="gateway-table-wrap"><table className="gateway-table">
        <thead><tr><th>{t("offline.colPipeline")}</th><th>{t("offline.colLadder")}</th><th>{t("offline.colEndpoint")}</th><th>{t("offline.colThresholds")}</th><th>{t("offline.colStatus")}</th><th className="numeric">{t("offline.colRequests")}</th><th>{t("offline.colUpdated")}</th><th /></tr></thead>
        <tbody>{pipelines.map((item) => <tr key={item.id}>
          <td className="provider-connection-identity"><strong>{item.name}</strong><small>{item.slug}</small></td>
          <td><div className="pipeline-ladder-cell">{item.models.map((model) => <small key={model.model_id}>{tierName(model.tier, t)} · <b>{model.name}</b></small>)}</div></td>
          <td><div className="pipeline-ladder-cell">{pipelineEndpoints(item).map((endpoint) => <small key={endpoint.id}><Link href="/my-endpoints"><b>{endpoint.name}</b></Link>{endpoint.tiers.length < item.models.length ? ` · ${endpoint.tiers.map((tier) => tierName(tier, t)).join(", ")}` : null}</small>)}</div></td>
          <td>{formatThreshold(item.classifier_mid_difficulty)} · {formatThreshold(item.classifier_large_difficulty)}<br /><small>{presetLabel(presetFor(item.classifier_mid_difficulty, item.classifier_large_difficulty), t)}</small></td>
          <td>{statusChip(item.status, t)}</td>
          <td className="numeric">{item.request_count.toLocaleString()}</td>
          <td><small>{new Date(item.updated_at).toLocaleDateString()}</small></td>
          <td className="endpoint-model-actions"><Link className="gateway-button gateway-button--ghost" href={`/offline/${item.id}`}>{t("offline.open")}</Link><Link className="gateway-button gateway-button--ghost" href={`/offline/${item.id}/playground`}>{t("offline.playground")}</Link>{canManage && <button type="button" className="gateway-button gateway-button--ghost" disabled={deletingId === item.id} onClick={() => void remove(item)}>{t("offline.remove")}</button>}</td>
        </tr>)}</tbody>
      </table></div>
    </section>}
  </>;
}

// ---- /offline/new and /offline/<id>/settings -------------------------------------------------------------

const EMPTY_DRAFT: OfflineDraft = {
  name: "", slug: "", description: "", models: {}, mid: 0.23, large: 0.61, maxOutputTokens: 800, timeoutSeconds: 60, fallbackEnabled: true,
};

export function OfflinePipelineEditorScene({ pipelineId, permissions }: { pipelineId?: string; permissions: readonly string[] }) {
  const { t } = useLanguage();
  const router = useRouter();
  const editing = Boolean(pipelineId);
  const [endpoints, setEndpoints] = useState<CustomerEndpoint[]>([]);
  const [existing, setExisting] = useState<OfflinePipeline | null>(null);
  const [draft, setDraft] = useState<OfflineDraft>(EMPTY_DRAFT);
  const [slugTouched, setSlugTouched] = useState(false);
  const [status, setStatus] = useState<"active" | "paused">("active");
  const [state, setState] = useState<"loading" | "ready" | "saving">("loading");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([listMyEndpoints(), pipelineId ? getOfflinePipeline(pipelineId) : Promise.resolve(null)])
      .then(([mine, pipeline]) => {
        if (!active) return;
        setEndpoints(mine);
        if (pipeline) {
          setExisting(pipeline);
          setStatus(pipeline.status === "paused" ? "paused" : "active");
          setDraft({
            name: pipeline.name, slug: pipeline.slug, description: pipeline.description ?? "",
            models: Object.fromEntries(pipeline.models.map((model) => [model.tier, model.model_id])),
            mid: pipeline.classifier_mid_difficulty, large: pipeline.classifier_large_difficulty,
            maxOutputTokens: pipeline.max_output_tokens, timeoutSeconds: pipeline.timeout_seconds, fallbackEnabled: pipeline.fallback_enabled,
          });
        }
        setState("ready");
      })
      .catch((reason: unknown) => { if (!active) return; setError(offlineErrorMessage(reason, t)); setState("ready"); });
    return () => { active = false; };
  }, [pipelineId, t]);

  const options = useMemo(() => Object.fromEntries(TIERS.map((tier) => {
    const list = tierOptions(endpoints, tier);
    // When editing, keep the current model selectable even if it is no longer active.
    const current = existing?.models.find((model) => model.tier === tier);
    if (current && !list.some((item) => item.id === current.model_id)) {
      list.unshift({ id: current.model_id, label: `${current.name} · ${current.endpoint_name}`, health: current.health_status, context: current.context_window });
    }
    return [tier, list];
  })) as Record<EndpointQualityTier, ReturnType<typeof tierOptions>>, [endpoints, existing]);

  if (state === "loading") return <FormPageSkeleton label={t("offline.title")} fields={6} />;

  const update = (patch: Partial<OfflineDraft>) => setDraft((current) => ({ ...current, ...patch }));
  const preset = presetFor(draft.mid, draft.large);
  const presetNote = t(preset === "saver" ? "offline.presetSaverNote" : preset === "balanced" ? "offline.presetBalancedNote" : preset === "quality" ? "offline.presetQualityNote" : "offline.presetCustomNote");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const problem = draftProblem(draft);
    if (problem) { setError(t(problem)); return; }
    setState("saving"); setError(null); setMessage(null);
    const models = draft.models as OfflineLadder;
    const shared = {
      name: draft.name.trim(), description: draft.description.trim() || null, models,
      classifier_mid_difficulty: draft.mid, classifier_large_difficulty: draft.large,
      max_output_tokens: draft.maxOutputTokens, timeout_seconds: draft.timeoutSeconds, fallback_enabled: draft.fallbackEnabled,
    };
    try {
      if (pipelineId) {
        const saved = await updateOfflinePipeline(pipelineId, { ...shared, status });
        setExisting(saved);
        setMessage(t("offline.saved"));
        setState("ready");
      } else {
        const created = await createOfflinePipeline({ ...shared, slug: draft.slug });
        router.push(`/offline/${created.id}`);
      }
    } catch (reason) {
      setError(offlineErrorMessage(reason, t));
      setState("ready");
    }
  }

  async function remove() {
    if (!pipelineId || !existing || !window.confirm(t("offline.deleteConfirm", { name: existing.name }))) return;
    setState("saving");
    try { await deleteOfflinePipeline(pipelineId); router.push("/offline"); }
    catch (reason) { setError(offlineErrorMessage(reason, t)); setState("ready"); }
  }

  const canManage = permissions.includes("applications.manage_own") && permissions.includes("model_pools.manage_own");
  const uses: Record<EndpointQualityTier, string> = { simple: t("offline.useSmall"), mid: t("offline.useMid"), hard: t("offline.useHard") };

  return <>
    <section className="gateway-page-heading">
      <div>
        <p className="gateway-eyebrow">{t("offline.title")}</p>
        <h1>{editing ? existing?.name ?? t("offline.editTitle") : t("offline.createTitle")}</h1>
        <p>{editing ? t("offline.editTitle") : t("offline.createDescription")}</p>
      </div>
    </section>
    {editing && existing && <nav className="application-tabs" aria-label={t("offline.title")}>
      <Link href={`/offline/${existing.id}`}>{t("offline.overview")}</Link>
      <Link href={`/offline/${existing.id}/playground`}>{t("offline.playground")}</Link>
      <Link className="is-active" href={`/offline/${existing.id}/settings`}>{t("offline.settings")}</Link>
    </nav>}
    {error && <div className="notice notice--warning" role="alert">{error}</div>}
    {message && <div className="notice" role="status">{message}</div>}
    {coveredTiers(endpoints) < 3 && !editing && <div className="notice notice--warning" role="note">{t("offline.needTiers")} <Link href="/my-endpoints">{t("offline.addEndpoints")}</Link></div>}

    <form className="gateway-panel form-panel" onSubmit={(event) => void submit(event)}>
      <section className="form-section">
        <div><h2>{t("offline.sectionInfo")}</h2><p>{t("offline.sectionInfoHelp")}</p></div>
        <div className="form-grid">
          <label className="field"><span>{t("offline.name")}</span><input required maxLength={200} value={draft.name} onChange={(event) => update({ name: event.target.value, ...(!editing && !slugTouched ? { slug: slugify(event.target.value) } : {}) })} /></label>
          <label className="field"><span>{t("offline.slug")}</span><input required maxLength={100} value={draft.slug} disabled={editing} onChange={(event) => { setSlugTouched(true); update({ slug: event.target.value }); }} />{editing && <small>{t("offline.slugLocked")}</small>}</label>
          <label className="field full"><span>{t("offline.descriptionField")}</span><input maxLength={2000} value={draft.description} onChange={(event) => update({ description: event.target.value })} /></label>
        </div>
      </section>

      <section className="form-section model-pool-members-section">
        <div><h2>{t("offline.sectionModels")}</h2><p>{t("offline.sectionModelsHelp")} <Link href="/my-endpoints">{t("nav.myEndpoints")}</Link>.</p></div>
        <ul className="model-pool-members">{TIERS.map((tier) => {
          const choices = options[tier];
          const chosen = choices.find((item) => item.id === draft.models[tier]);
          return <li key={tier}>
            <div>
              <strong>{tierName(tier, t)}{chosen ? ` — ${chosen.label.split(" · ")[0]}` : ""}</strong>
              <small>{uses[tier]}</small>
              <small>{chosen ? `${chosen.label.split(" · ").slice(1).join(" · ")} · ${chosen.health}${chosen.context ? ` · ${chosen.context.toLocaleString()} tokens` : ""}` : choices.length ? t("offline.pickModel") : t("offline.noModelInTier")}</small>
            </div>
            <div className="member-actions">
              {choices.length === 0 && canManage
                ? <Link className="gateway-button gateway-button--ghost" href="/my-endpoints">{t("offline.addModels")}</Link>
                : <select aria-label={`${tierName(tier, t)} — ${t("offline.pickModel")}`} value={draft.models[tier] ?? ""} disabled={!canManage || choices.length === 0} onChange={(event) => update({ models: { ...draft.models, [tier]: event.target.value || undefined } })}>
                  <option value="">{t("offline.pickModel")}</option>
                  {choices.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                </select>}
            </div>
          </li>;
        })}</ul>
      </section>

      <section className="form-section">
        <div><h2>{t("offline.sectionRouting")}</h2><p>{t("offline.sectionRoutingHelp")}</p></div>
        <div>
          <div className="pipeline-presets" role="radiogroup" aria-label={t("offline.presetGroup")}>
            {THRESHOLD_PRESETS.map((item) => <button key={item.key} type="button" role="radio" aria-checked={preset === item.key} className={preset === item.key ? "is-active" : undefined} onClick={() => update({ mid: item.mid, large: item.large })}>{presetLabel(item.key, t)}</button>)}
          </div>
          <p className="pipeline-preset-note">{presetNote}</p>
          <ThresholdBar mid={Math.min(Math.max(draft.mid, 0), 1)} large={Math.min(Math.max(draft.large, 0), 1)} />
          <div className="form-grid pipeline-threshold-fields">
            <label className="field"><span>{t("offline.midThreshold")}</span><input type="number" step="0.01" min={0} max={1} value={draft.mid} onChange={(event) => update({ mid: Number.parseFloat(event.target.value) })} /></label>
            <label className="field"><span>{t("offline.largeThreshold")}</span><input type="number" step="0.01" min={0} max={1} value={draft.large} onChange={(event) => update({ large: Number.parseFloat(event.target.value) })} /></label>
          </div>
        </div>
      </section>

      <section className="form-section">
        <div><h2>{t("offline.sectionLimits")}</h2><p>{t("offline.sectionLimitsHelp")}</p></div>
        <div className="form-grid">
          <label className="field"><span>{t("offline.maxTokens")}</span><input type="number" min={1} max={32768} value={draft.maxOutputTokens} onChange={(event) => update({ maxOutputTokens: Number.parseInt(event.target.value, 10) || 1 })} /></label>
          <label className="field"><span>{t("offline.timeout")}</span><input type="number" min={1} max={600} value={draft.timeoutSeconds} onChange={(event) => update({ timeoutSeconds: Number.parseFloat(event.target.value) || 1 })} /></label>
          <label className="field full"><span>{t("offline.onFailure")}</span><select value={draft.fallbackEnabled ? "fallback" : "stop"} onChange={(event) => update({ fallbackEnabled: event.target.value === "fallback" })}>
            <option value="fallback">{t("offline.onFailureFallback")}</option><option value="stop">{t("offline.onFailureStop")}</option>
          </select></label>
          {editing && <label className="field full"><span>{t("offline.statusField")}</span><select value={status} onChange={(event) => setStatus(event.target.value as "active" | "paused")}>
            <option value="active">{t("offline.statusActive")}</option><option value="paused">{t("offline.statusPaused")}</option>
          </select></label>}
        </div>
      </section>

      {editing && canManage && <section className="form-section">
        <div><h2>{t("offline.dangerTitle")}</h2><p>{t("offline.dangerHelp")}</p></div>
        <div><button type="button" className="gateway-button gateway-button--ghost" disabled={state === "saving"} onClick={() => void remove()}>{t("offline.delete")}</button></div>
      </section>}

      <div className="form-actions">
        <Link className="gateway-button gateway-button--ghost" href={editing && existing ? `/offline/${existing.id}` : "/offline"}>{t("offline.cancel")}</Link>
        <button className="gateway-button" type="submit" disabled={!canManage || state === "saving"}>{state === "saving" ? t("offline.saving") : editing ? t("offline.save") : t("offline.create")}</button>
      </div>
    </form>
  </>;
}

// ---- /offline/<id> -------------------------------------------------------------------------------------

export function offlineSnippet(baseUrl: string): string {
  return `import uuid
from openai import OpenAI

client = OpenAI(base_url="${baseUrl}", api_key="off_...")
reply = client.chat.completions.create(
    model="auto",  # the classifier picks Small / Mid / Large
    messages=[{"role": "user", "content": "Summarize these meeting notes..."}],
    extra_headers={"Idempotency-Key": str(uuid.uuid4())},  # required on every generation request
)
print(reply.choices[0].message.content)`;
}

export function OfflinePipelineDetailScene({ id, permissions }: { id?: string; permissions: readonly string[] }) {
  const { t } = useLanguage();
  const [pipeline, setPipeline] = useState<OfflinePipeline | null>(null);
  const [recent, setRecent] = useState<GatewayRequestSummary[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const canManage = permissions.includes("applications.manage_own") && permissions.includes("model_pools.manage_own");
  const canReadRequests = permissions.some((permission) => ["requests.read_own", "requests.read_global", "usage.read_own", "usage.read_global"].includes(permission));

  useEffect(() => {
    if (!id) return;
    let active = true;
    Promise.all([getOfflinePipeline(id), canReadRequests ? listGatewayRequests({ application_id: id, page: 1, page_size: 5 }) : Promise.resolve(null)])
      .then(([value, requests]) => { if (!active) return; setPipeline(value); setRecent(requests?.items ?? []); setState("ready"); })
      .catch((reason: unknown) => { if (!active) return; setError(offlineErrorMessage(reason, t)); setState("error"); });
    return () => { active = false; };
  }, [canReadRequests, id, t]);

  if (state === "loading") return <DetailSkeleton label={t("offline.title")} />;
  if (!pipeline) return <div className="notice notice--warning" role="alert">{error ?? t("offline.notFound")}</div>;

  const baseUrl = gatewayOpenAIBaseUrl();
  const snippet = offlineSnippet(baseUrl);
  const endpointNames = [...new Set(pipeline.models.map((model) => model.endpoint_name))].join(", ");
  const preset = presetFor(pipeline.classifier_mid_difficulty, pipeline.classifier_large_difficulty);

  return <>
    <section className="gateway-page-heading">
      <div><p className="gateway-eyebrow">{t("offline.title")}</p><h1>{pipeline.name}</h1><p>{pipeline.slug} · {t("offline.version", { version: pipeline.config_version ?? 1 })}</p></div>
      {canManage && <Link href={`/offline/${pipeline.id}/settings`} className="gateway-button gateway-button--secondary">{t("offline.configure")}</Link>}
    </section>
    <nav className="application-tabs" aria-label={t("offline.title")}>
      <Link className="is-active" href={`/offline/${pipeline.id}`}>{t("offline.overview")}</Link>
      <Link href={`/offline/${pipeline.id}/playground`}>{t("offline.playground")}</Link>
      {canManage && <Link href={`/offline/${pipeline.id}/settings`}>{t("offline.settings")}</Link>}
    </nav>

    <section className="gateway-policy-grid">
      <article><span>{t("offline.statLadder")}</span><strong>{t("offline.statLadderValue")}</strong><small>{pipeline.models.map((model) => model.name).join(" · ")}</small></article>
      <article><span>{t("offline.statThresholds")}</span><strong>{formatThreshold(pipeline.classifier_mid_difficulty)} · {formatThreshold(pipeline.classifier_large_difficulty)}</strong><small>{presetLabel(preset, t)}</small></article>
      <article><span>{t("offline.statRequests")}</span><strong>{pipeline.request_count.toLocaleString()}</strong><small>{t("offline.statRequestsDetail")}</small></article>
      <article><span>{t("offline.statLimits")}</span><strong>{t("offline.statLimitsValue", { tokens: pipeline.max_output_tokens.toLocaleString() })}</strong><small>{t("offline.statLimitsDetail", { seconds: pipeline.timeout_seconds, fallback: pipeline.fallback_enabled ? t("offline.fallbackOn") : t("offline.fallbackOff") })}</small></article>
    </section>
    {pipeline.status !== "active" && <div className="notice notice--warning" role="status">{t("offline.pausedNotice")}</div>}

    <section className="gateway-panel">
      <div className="gateway-section-heading"><div><h2>{t("offline.connectionTitle")}</h2><p>{t("offline.connectionHelp")}</p></div></div>
      <div className="pipeline-connection">
        <div><span>{t("offline.baseUrl")}</span><code>{baseUrl}</code></div>
        <div><span>{t("offline.model")}</span><code>auto</code></div>
      </div>
      <div className="sdk-code-card">
        <div className="sdk-code-toolbar sdk-code-toolbar--snippet"><span>{t("offline.firstRequest")}</span><button type="button" className="sdk-copy-button" onClick={() => void navigator.clipboard.writeText(snippet).then(() => setCopied(true))}>{copied ? t("offline.copied") : t("offline.copy")}</button></div>
        <pre><code>{snippet}</code></pre>
      </div>
    </section>

    <ApplicationKeyPanel applicationId={pipeline.id} canManage={canManage} />

    <section className="gateway-panel">
      <div className="gateway-section-heading"><div><h2>{t("offline.stepsTitle")}</h2><p>{t("offline.stepsHelp")}</p></div></div>
      <ol className="pipeline-steps">
        <li><span>Understand</span><strong>{t("offline.stepUnderstand")}</strong><small>{t("offline.stepUnderstandDetail")}</small></li>
        <li><span>Decompose</span><strong>{t("offline.stepDecompose")}</strong><small>{t("offline.stepDecomposeDetail")}</small></li>
        <li><span>Route</span><strong>{t("offline.stepRoute")}</strong><small>{t("offline.stepRouteDetail", { mid: formatThreshold(pipeline.classifier_mid_difficulty), large: formatThreshold(pipeline.classifier_large_difficulty) })}</small></li>
        <li><span>Execute</span><strong>{t("offline.stepExecute")}</strong><small>{endpointNames}</small></li>
        <li><span>Aggregate</span><strong>{t("offline.stepAggregate")}</strong><small>{t("offline.stepAggregateDetail")}</small></li>
      </ol>
      <p className="pipeline-steps-note">{t("offline.stepsNote")}</p>
    </section>

    <section className="gateway-panel table-panel">
      <div className="gateway-section-heading"><div><h2>{t("offline.ladderTitle")}</h2><p>{t("offline.ladderHelp")}</p></div></div>
      <div className="gateway-table-wrap"><table className="gateway-table">
        <thead><tr><th>{t("offline.colTier")}</th><th>{t("offline.colModel")}</th><th>{t("offline.colEndpoint")}</th><th>{t("offline.colStatus")}</th></tr></thead>
        <tbody>{pipeline.models.map((model) => <tr key={model.model_id}>
          <td>{tierName(model.tier, t)}</td>
          <td className="provider-connection-identity"><strong>{model.name}</strong>{model.context_window ? <small>{model.context_window.toLocaleString()} tokens</small> : null}</td>
          <td>{model.endpoint_name}</td>
          <td><span className={"gateway-status gateway-status--" + (model.health_status === "healthy" ? "healthy" : model.status === "active" ? "neutral" : "watch")}><i className="status-pin" />{model.health_status}</span></td>
        </tr>)}</tbody>
      </table></div>
    </section>

    {canReadRequests && <section className="gateway-panel application-recent-requests" aria-labelledby="offline-recent-title">
      <div className="gateway-section-heading"><div><h2 id="offline-recent-title">{t("offline.recentTitle")}</h2><p>{t("offline.recentHelp")}</p></div><Link href={`/requests?search=${encodeURIComponent(pipeline.name)}`}>{t("offline.viewAll")}</Link></div>
      {recent.length === 0 ? <div className="notice">{t("offline.recentNone")}</div> : <div className="application-recent-request-list">{recent.map((request) => <Link key={request.request_id} href={`/requests/${encodeURIComponent(request.request_id)}`}>
        <span><strong>{request.request_id}</strong><small>{request.source} / {new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(request.started_at))}</small></span>
        <span className={`gateway-status gateway-status--${request.status === "completed" ? "healthy" : request.status === "failed" ? "incident" : "watch"}`}><i className="status-pin" aria-hidden="true" />{request.status}</span>
        <span className="application-recent-request-metrics">{request.total_latency_ms.toFixed(0)}ms / {request.total_cost.toFixed(4)}</span>
      </Link>)}</div>}
    </section>}
  </>;
}
