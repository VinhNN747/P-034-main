"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { FormPageSkeleton } from "@/components/gateway/gateway-page-skeletons";
import { OfflineSectionTabs } from "@/components/gateway/offline-pipeline-scenes";
import { useLanguage } from "@/components/language-provider";
import {
  addMyEndpointModel,
  createMyEndpoint,
  deleteMyEndpoint,
  deleteMyEndpointModel,
  discoverMyEndpointModels,
  listMyEndpoints,
  probeMyEndpointModel,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type { CustomerEndpoint, EndpointModel, EndpointQualityTier } from "@/lib/gateway-types";

type Translate = ReturnType<typeof useLanguage>["t"];

/** The backend explains a refused URL or a conflict in plain words (422/409); other failures stay generic. */
export function endpointErrorMessage(error: unknown, t: Translate): string {
  if (error && typeof error === "object" && "status" in error) {
    const apiError = error as GatewayApiError;
    if (apiError.status === 401 || apiError.status === 403) return t("endpoints.errorPermission");
    if (apiError.status === 503) return t("endpoints.errorUnavailable");
    if ((apiError.status === 409 || apiError.status === 422 || apiError.status === 404) && apiError.message) {
      return apiError.message;
    }
  }
  return t("endpoints.errorGeneric");
}

export function activeTierCount(endpoints: CustomerEndpoint[]): number {
  const tiers = new Set(endpoints.flatMap((item) => item.models.filter((model) => model.status === "active").map((model) => model.quality_tier)));
  return ["simple", "mid", "hard"].filter((tier) => tiers.has(tier)).length;
}

function tierLabel(tier: string, t: Translate) {
  if (tier === "simple") return t("endpoints.tierSimple");
  if (tier === "hard") return t("endpoints.tierHard");
  return t("endpoints.tierMid");
}

function healthTone(model: EndpointModel) {
  if (model.health_status === "healthy") return "healthy";
  if (model.health_status === "unhealthy" || model.health_status === "unavailable") return "watch";
  return "neutral";
}

type Discovery = { state: "idle" | "loading" } | { state: "done"; models: string[] } | { state: "error"; message: string };

function AddModelForm({ endpoint, pending, onAdd }: {
  endpoint: CustomerEndpoint;
  pending: boolean;
  onAdd: (input: { name: string; quality_tier: EndpointQualityTier; context_window?: number }) => Promise<boolean>;
}) {
  const { t } = useLanguage();
  const [name, setName] = useState("");
  const [tier, setTier] = useState<EndpointQualityTier>("simple");
  const [contextWindow, setContextWindow] = useState("");
  const [discovery, setDiscovery] = useState<Discovery>({ state: "idle" });
  const listId = `endpoint-${endpoint.id}-served`;
  const hasModels = endpoint.models.length > 0;

  const discover = useCallback(async () => {
    setDiscovery({ state: "loading" });
    try {
      setDiscovery({ state: "done", models: await discoverMyEndpointModels(endpoint.id) });
    } catch (error) {
      setDiscovery({ state: "error", message: endpointErrorMessage(error, t) });
    }
  }, [endpoint.id, t]);

  // A fresh endpoint has nothing to pick yet: ask the server what it serves straight away.
  useEffect(() => {
    if (hasModels) return;
    const timer = window.setTimeout(() => { void discover(); }, 0);
    return () => window.clearTimeout(timer);
  }, [discover, hasModels]);

  const added = new Set(endpoint.models.map((model) => model.name));
  const served = discovery.state === "done" ? discovery.models : [];
  const notAdded = served.filter((model) => !added.has(model));

  return <form className="endpoint-model-form" aria-labelledby={`endpoint-${endpoint.id}-add`} onSubmit={(event) => {
    event.preventDefault();
    const windowValue = Number.parseInt(contextWindow, 10);
    void onAdd({ name: name.trim(), quality_tier: tier, ...(Number.isFinite(windowValue) ? { context_window: windowValue } : {}) })
      .then((ok) => { if (ok) { setName(""); setContextWindow(""); } });
  }}>
    <h3 id={`endpoint-${endpoint.id}-add`}>{t("endpoints.addModel")}</h3>
    <div className="endpoint-discovery">
      <span className="endpoint-discovery-label">{t("endpoints.servedModels")}</span>
      <div className="endpoint-discovery-row">
        <button type="button" className="gateway-button gateway-button--ghost" disabled={discovery.state === "loading"} onClick={() => { void discover(); }}>
          {discovery.state === "loading" ? t("endpoints.discovering") : t("endpoints.discover")}
        </button>
        <small role="status" className={discovery.state === "error" ? "endpoint-probe-error" : undefined}>
          {discovery.state === "error" ? discovery.message
            : discovery.state === "done" ? (served.length ? t("endpoints.discovered").replace("{count}", String(served.length)) : t("endpoints.discoveredNone"))
              : null}
        </small>
      </div>
      {notAdded.length > 0 && <div className="endpoint-discovery-chips">
        {notAdded.map((model) => <button key={model} type="button" aria-pressed={name === model} className={name === model ? "is-selected" : undefined} onClick={() => setName(model)}>{model}</button>)}
      </div>}
    </div>
    <div className="endpoint-model-fields">
    <label className="field"><span>{t("endpoints.modelName")}</span><input required maxLength={150} list={listId} value={name} onChange={(event) => setName(event.target.value)} placeholder="qwen2.5:1.5b-instruct" /></label>
    <datalist id={listId}>{notAdded.map((model) => <option key={model} value={model} />)}</datalist>
    <label className="field"><span>{t("endpoints.tier")}</span><select value={tier} onChange={(event) => setTier(event.target.value as EndpointQualityTier)}>
      <option value="simple">{t("endpoints.tierSimple")}</option>
      <option value="mid">{t("endpoints.tierMid")}</option>
      <option value="hard">{t("endpoints.tierHard")}</option>
    </select></label>
    <label className="field"><span>{t("endpoints.contextWindow")}</span><input type="number" min={256} inputMode="numeric" value={contextWindow} onChange={(event) => setContextWindow(event.target.value)} placeholder="4096" /></label>
    <button className="gateway-button" type="submit" disabled={pending || !name.trim()}>{t("endpoints.addModel")}</button>
    </div>
  </form>;
}

function EndpointCard({ endpoint, pendingId, error, message, onAddModel, onProbe, onProbeAll, onRemoveModel, onDelete }: {
  endpoint: CustomerEndpoint;
  pendingId: string | null;
  /** Feedback from an action on this card, shown here rather than at the top of a long page. */
  error: string | null;
  message: string | null;
  onAddModel: (endpoint: CustomerEndpoint, input: { name: string; quality_tier: EndpointQualityTier; context_window?: number }) => Promise<boolean>;
  onProbe: (endpoint: CustomerEndpoint, model: EndpointModel) => void;
  onProbeAll: (endpoint: CustomerEndpoint) => void;
  onRemoveModel: (endpoint: CustomerEndpoint, model: EndpointModel) => void;
  onDelete: (endpoint: CustomerEndpoint) => void;
}) {
  const { t } = useLanguage();
  const busy = pendingId === endpoint.id || endpoint.models.some((model) => model.id === pendingId);
  return <section className="gateway-panel table-panel endpoint-card" aria-labelledby={`endpoint-${endpoint.id}-title`}>
    <div className="gateway-page-heading provider-section-heading">
      <div>
        <p className="gateway-eyebrow">{t("endpoints.free")}</p>
        <h2 id={`endpoint-${endpoint.id}-title`}>{endpoint.display_name}</h2>
        <p><code>{endpoint.base_url}</code> · {t("endpoints.key")}: {endpoint.has_api_key ? `••••${endpoint.key_last4 ?? ""}` : t("endpoints.noKey")}</p>
      </div>
      <div className="endpoint-model-actions">
        {endpoint.models.length > 0 && <button type="button" className="gateway-button gateway-button--ghost" disabled={busy} onClick={() => onProbeAll(endpoint)}>{t("endpoints.probeAll")}</button>}
        <button type="button" className="gateway-button gateway-button--ghost" disabled={busy} onClick={() => onDelete(endpoint)}>{t("endpoints.delete")}</button>
      </div>
    </div>
    {error && <div className="notice notice--warning endpoint-card-notice" role="alert">{error}</div>}
    {message && <div className="notice endpoint-card-notice" role="status">{message}</div>}
    {endpoint.models.length === 0 ? <div className="notice">{t("endpoints.noModels")}</div> : <div className="gateway-table-wrap"><table className="gateway-table">
      <thead><tr><th>{t("endpoints.models")}</th><th>{t("endpoints.tier")}</th><th>{t("endpoints.status")}</th><th>{t("endpoints.health")}</th><th>{t("endpoints.lastProbe")}</th><th>{t("endpoints.actions")}</th></tr></thead>
      <tbody>{endpoint.models.map((model) => {
        const probing = pendingId === model.id;
        return <tr key={model.id}>
          <td className="provider-connection-identity"><strong>{model.name}</strong>{model.context_window ? <small>{model.context_window.toLocaleString()} tokens</small> : null}</td>
          <td>{tierLabel(model.quality_tier, t)}</td>
          <td><span className={"provider-connection-status provider-connection-status--" + model.status}>{model.status}</span></td>
          <td><span className={"gateway-status gateway-status--" + healthTone(model)}><i className="status-pin" />{model.health_status}</span>{model.last_probe_error && <small className="endpoint-probe-error">{model.last_probe_error}</small>}</td>
          <td><small>{model.last_probe_at ? new Date(model.last_probe_at).toLocaleString() : t("endpoints.never")}</small></td>
          <td className="endpoint-model-actions">
            <button type="button" className="gateway-button gateway-button--ghost" disabled={probing || busy} onClick={() => onProbe(endpoint, model)}>{probing ? t("endpoints.probing") : t("endpoints.probe")}</button>
            <button type="button" className="gateway-button gateway-button--ghost" disabled={probing || busy} onClick={() => onRemoveModel(endpoint, model)}>{t("endpoints.removeModel")}</button>
          </td>
        </tr>;
      })}</tbody>
    </table></div>}
    <AddModelForm endpoint={endpoint} pending={busy} onAdd={(input) => onAddModel(endpoint, input)} />
  </section>;
}

export function CustomerEndpointsScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const canManage = permissions.includes("model_pools.manage_own");
  const [endpoints, setEndpoints] = useState<CustomerEndpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  // The endpoint whose card should show error/message; null shows them at the top of the page.
  const [feedbackFor, setFeedbackFor] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");

  const replace = useCallback((updated: CustomerEndpoint) => {
    setEndpoints((current) => current.map((item) => item.id === updated.id ? updated : item));
  }, []);

  useEffect(() => {
    let active = true;
    listMyEndpoints()
      .then((items) => { if (active) setEndpoints(items); })
      .catch((reason: unknown) => { if (active) setError(endpointErrorMessage(reason, t)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [t]);

  async function run<T>(id: string, action: () => Promise<T>, endpointId: string | null = null): Promise<T | undefined> {
    setPendingId(id); setError(null); setMessage(null); setFeedbackFor(endpointId);
    try { return await action(); }
    catch (reason) { setError(endpointErrorMessage(reason, t)); return undefined; }
    finally { setPendingId(null); }
  }

  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const created = await run("create", () => createMyEndpoint({
      display_name: displayName.trim(),
      base_url: baseUrl.trim(),
      ...(apiKey ? { api_key: apiKey } : {}),
    }));
    setApiKey("");
    if (!created) return;
    setEndpoints((current) => [...current, created]);
    setDisplayName(""); setBaseUrl("");
    setMessage(t("endpoints.created"));
  }

  async function addModel(endpoint: CustomerEndpoint, input: { name: string; quality_tier: EndpointQualityTier; context_window?: number }) {
    const updated = await run(endpoint.id, () => addMyEndpointModel(endpoint.id, input), endpoint.id);
    if (!updated) return false;
    replace(updated);
    // A new model starts inactive; check it straight away so it can be used without a second click.
    const added = updated.models.find((item) => item.name === input.name);
    if (added) await probe(updated, added);
    return true;
  }

  async function probe(endpoint: CustomerEndpoint, model: EndpointModel) {
    const updated = await run(model.id, () => probeMyEndpointModel(endpoint.id, model.id), endpoint.id);
    if (!updated) return;
    replace(updated);
    const after = updated.models.find((item) => item.id === model.id);
    if (after?.status === "active") setMessage(t("endpoints.probeHealthy", { name: model.name }));
    else setError([t("endpoints.probeFailed", { name: model.name }), after?.last_probe_error].filter(Boolean).join(" "));
  }

  async function probeAll(endpoint: CustomerEndpoint) {
    let latest = endpoint;
    const failed: string[] = [];
    setFeedbackFor(endpoint.id); setError(null); setMessage(null);
    for (const model of endpoint.models) {
      setPendingId(model.id);
      try {
        latest = await probeMyEndpointModel(endpoint.id, model.id);
        replace(latest);
        if (latest.models.find((item) => item.id === model.id)?.status !== "active") failed.push(model.name);
      } catch (reason) {
        setError(endpointErrorMessage(reason, t));
        setPendingId(null);
        return;
      }
    }
    setPendingId(null);
    if (failed.length) setError(t("endpoints.probeAllFailed", { names: failed.join(", ") }));
    else setMessage(t("endpoints.probeAllHealthy", { count: endpoint.models.length }));
  }

  async function removeModel(endpoint: CustomerEndpoint, model: EndpointModel) {
    const updated = await run(endpoint.id, () => deleteMyEndpointModel(endpoint.id, model.id), endpoint.id);
    if (updated) replace(updated);
  }

  async function remove(endpoint: CustomerEndpoint) {
    if (!window.confirm(t("endpoints.deleteConfirm", { name: endpoint.display_name }))) return;
    // Feedback goes on the card: a refusal (models still used by a pipeline) must show next to the button.
    const done = await run(endpoint.id, async () => { await deleteMyEndpoint(endpoint.id); return true; }, endpoint.id);
    if (done) setEndpoints((current) => current.filter((item) => item.id !== endpoint.id));
  }

  if (loading) return <FormPageSkeleton label={t("endpoints.title")} fields={3} />;

  return <>
    <section className="gateway-page-heading"><div><p className="gateway-eyebrow">{t("endpoints.eyebrow")}</p><h1>{t("endpoints.title")}</h1><p>{t("endpoints.description")}</p></div></section>
    <OfflineSectionTabs active="/my-endpoints" permissions={permissions} />
    {feedbackFor === null && error && <div className="notice notice--warning" role="alert">{error}</div>}
    {feedbackFor === null && message && <div className="notice" role="status">{message}</div>}

    {canManage && <section className="gateway-panel endpoint-create-panel" aria-labelledby="endpoint-create-title">
      <h2 id="endpoint-create-title">{t("endpoints.add")}</h2>
      <p>{t("endpoints.rules")}</p>
      <form className="form-grid" onSubmit={(event) => void create(event)}>
        <label className="field"><span>{t("endpoints.displayName")}</span><input required maxLength={200} value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="My vLLM" /></label>
        <label className="field"><span>{t("endpoints.baseUrl")}</span><input required type="url" maxLength={500} value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://gpu.example.com/v1" /></label>
        <label className="field full"><span>{t("endpoints.apiKey")}</span><input type="password" autoComplete="new-password" minLength={5} value={apiKey} onChange={(event) => setApiKey(event.target.value)} /></label>
        <div className="form-actions"><button className="gateway-button" type="submit" disabled={pendingId === "create"}>{pendingId === "create" ? t("endpoints.saving") : t("endpoints.add")}</button></div>
      </form>
    </section>}

    {endpoints.length === 0 ? <div className="notice">{t("endpoints.none")}</div> : endpoints.map((endpoint) => <EndpointCard
      key={endpoint.id}
      endpoint={endpoint}
      pendingId={pendingId}
      error={feedbackFor === endpoint.id ? error : null}
      message={feedbackFor === endpoint.id ? message : null}
      onAddModel={addModel}
      onProbe={(item, model) => void probe(item, model)}
      onProbeAll={(item) => void probeAll(item)}
      onRemoveModel={(item, model) => void removeModel(item, model)}
      onDelete={(item) => void remove(item)}
    />)}

    {activeTierCount(endpoints) > 0 && <div className="notice" role="note">{t("endpoints.usePool")} <Link href="/model-pools/new">{t("endpoints.createPool")}</Link></div>}
  </>;
}
