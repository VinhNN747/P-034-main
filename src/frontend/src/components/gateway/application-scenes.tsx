"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLanguage } from "@/components/language-provider";

import {
  deleteApplication,
  getApplication,
  getGatewayConfig,
  listApplicationModels,
  listAvailableModels,
  listGatewayRequests,
  listModelPools,
  updateGatewayConfig,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type {
  ApplicationEnvironment,
  ApplicationDetail,
  ApplicationSortField,
  ApplicationSummary,
  GatewayConfig,
  GatewayRequestSummary,
  ModelPool,
  ModelSummary,
} from "@/lib/gateway-types";
import { listApplications } from "@/lib/gateway-api";

import { toApplicationViewModel } from "@/components/gateway/application-view-model";
import { ApplicationKeyPanel } from "@/components/gateway/application-key-panel";
import { OneTimeApiKeySecret } from "@/components/gateway/api-key-secret";
import { completeApplicationSetup } from "@/components/gateway/application-setup";
import { DeleteConfirmDialog } from "@/components/gateway/delete-confirm-dialog";
import { PaginationControls, TABLE_PAGE_SIZE } from "@/components/gateway/pagination-controls";
import { FilterBuilder, type FilterCondition } from "@/components/gateway/filter-builder";
import { DetailSkeleton, FormPageSkeleton, ListPageSkeleton } from "@/components/gateway/gateway-page-skeletons";
import { capabilityPresentation, modelDifficultyPresentation, modelHealthLabel, modelHealthTone } from "@/components/gateway/model-scenes";
import { SortPrioritySummary, SortableTableHeader, sortQueryParams, toggleSortCriterion, type SortCriterion } from "@/components/gateway/multi-sort";

function SceneState({
  message,
  tone = "neutral",
}: {
  message: string;
  tone?: "neutral" | "error";
}) {
  return (
    <div className={tone === "error" ? "notice notice--warning" : "notice"} role="status">
      {message}
    </div>
  );
}

function budgetMoney(value: number | null | undefined): string {
  if (value === null || value === undefined) return "Not set";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function budgetStateLabel(state: string | undefined): string {
  if (state === "warning") return "Warning threshold reached";
  if (state === "exhausted") return "Budget exhausted";
  if (state === "healthy") return "Within budget";
  return "Not configured";
}

// System task templates are ownerless pools; the default pool is the empty choice.
const isTemplatePool = (pool: ModelPool) => pool.owner_user_id === null && pool.purpose.startsWith("system-template:");

function ModelPoolField({
  pools,
  value,
  onChange,
  loading = false,
  className = "field",
}: {
  pools: ModelPool[];
  value: string;
  onChange: (value: string) => void;
  loading?: boolean;
  className?: string;
}) {
  const { t } = useLanguage();
  return <label className={className}>
    <span>{t("applications.modelPoolOptional")}</span>
    <select value={value} onChange={(event) => onChange(event.target.value)} disabled={loading}>
      <option value="">{loading ? t("applications.loadingPools") : t("applications.systemDefaultPool")}</option>
      {value && !pools.some((pool) => pool.id === value) && <option value={value} disabled>{t("applications.poolUnavailable")}</option>}
      {pools.some((pool) => pool.owner_user_id !== null) && <optgroup label={t("applications.yourPools")}>
        {pools.filter((pool) => pool.owner_user_id !== null).map((pool) => <option key={pool.id} value={pool.id}>{pool.name} · {pool.purpose}</option>)}
      </optgroup>}
      {pools.some((pool) => isTemplatePool(pool)) && <optgroup label={t("applications.systemTemplates")}>
        {pools.filter(isTemplatePool).map((pool) => <option key={pool.id} value={pool.id}>{pool.name}</option>)}
      </optgroup>}
    </select>
    <small>{t("applications.modelPoolHint")} <code>model=&quot;auto:code&quot;</code>.</small>
  </label>;
}

function apiMessage(error: unknown, fallback: string): string {
  return error && typeof error === "object" && "message" in error
    ? String((error as GatewayApiError).message)
    : fallback;
}

function applicationStatusTone(status: string): "healthy" | "watch" | "neutral" {
  if (status === "active") return "healthy";
  if (status === "paused") return "watch";
  return "neutral";
}

function ApplicationModelDifficulty({ qualityTier }: { qualityTier: string | null | undefined }) {
  const { t, language } = useLanguage();
  const difficulty = modelDifficultyPresentation(qualityTier, language);
  return <span className={`model-difficulty model-difficulty--${difficulty.tone}`} title={`${t("models.routingDifficulty")}: ${difficulty.label}`}>
    <i className="model-difficulty-icon" aria-hidden="true"><b /><b /><b /></i>
    {difficulty.label}
  </span>;
}

function ApplicationModelsDialog({
  application,
  onClose,
}: {
  application: ApplicationSummary;
  onClose: () => void;
}) {
  const { t, language } = useLanguage();
  const [models, setModels] = useState<ModelSummary[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void listApplicationModels(application.id)
      .then((response) => {
        if (!active) return;
        setModels(response.items);
        setState("ready");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(apiMessage(reason, t("applications.errorModels")));
        setState("error");
      });
    return () => { active = false; };
  }, [application.id, t]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  const grouped = models.reduce<Record<string, ModelSummary[]>>((result, model) => {
    (result[model.provider] ??= []).push(model);
    return result;
  }, {});
  const providerGroups = Object.entries(grouped).sort(([left], [right]) => left.localeCompare(right));
  const healthyModels = models.filter((model) => modelHealthTone(model.health_status) === "healthy").length;

  if (typeof document === "undefined") return null;

  return createPortal(
    <div className="model-capability-dialog-scrim application-model-dialog-scrim" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section className="model-capability-dialog application-model-dialog" role="dialog" aria-modal="true" aria-labelledby="application-models-title" aria-describedby="application-models-summary">
        <header>
          <div>
            <span>{t("applications.modelCatalogTitle")}</span>
            <h2 id="application-models-title">{t("applications.modelsRoutedFor", { name: application.name })}</h2>
            <p id="application-models-summary">{t("applications.modelCatalogDescription")}</p>
          </div>
          <button type="button" onClick={onClose} aria-label={t("applications.closeModelCatalog")} autoFocus>×</button>
        </header>
        <div className="application-model-dialog-body">
          {state === "ready" && models.length > 0 && <div className="application-model-overview" aria-label={t("applications.modelCatalogTitle")}>
            <div><strong>{models.length}</strong><span>{t("applications.availableModels")}</span></div>
            <div><strong>{providerGroups.length}</strong><span>{t("nav.providers")}</span></div>
            <div><strong>{healthyModels}</strong><span>{t("applications.healthyNow")}</span></div>
          </div>}
          {state === "loading" && <SceneState message={t("applications.loadingModels")} />}
          {state === "error" && <SceneState message={error ?? t("applications.errorModels")} tone="error" />}
          {state === "ready" && models.length === 0 && <SceneState message={t("applications.noModels")} />}
          {state === "ready" && providerGroups.map(([provider, providerModels]) => (
            <section className="application-model-group" key={provider}>
              <div className="application-model-group-heading">
                <h3>{provider}</h3>
                <span>{t("applications.modelCount", { count: providerModels.length })}</span>
              </div>
              <div className="application-model-list">
                {providerModels.map((model) => {
                  const capabilities = [...new Map(model.capabilities.map((capability) => {
                    const presented = capabilityPresentation(capability, language);
                    return [presented.label, presented];
                  })).values()];
                  const healthLabel = modelHealthLabel(model.health_status, language);
                  return <article className="application-model-card" key={model.id}>
                    <div className="application-model-card-heading">
                      <span className="application-model-provider-mark" aria-hidden="true">{provider.slice(0, 2).toUpperCase()}</span>
                      <div><strong>{model.display_name || model.name}</strong><small title={model.name}>{model.name}</small></div>
                      <span className={`gateway-status gateway-status--${model.status === "active" ? "healthy" : "watch"}`}><i className="status-pin" aria-hidden="true" />{model.status === "active" ? t("models.active") : t("models.inactive")}</span>
                    </div>
                    <div className="application-model-signals">
                      <ApplicationModelDifficulty qualityTier={model.quality_tier} />
                      <span className={`gateway-status model-health-badge gateway-status--${modelHealthTone(model.health_status)}`} title={`${t("models.healthLabel")}: ${healthLabel}`}><i className="status-pin" aria-hidden="true" />{healthLabel}</span>
                    </div>
                    <dl className="application-model-facts">
                      <div><dt>{t("applications.contextWindow")}</dt><dd>{model.context_window ? model.context_window.toLocaleString() : "—"}</dd></div>
                      <div><dt>{t("applications.targetLatency")}</dt><dd>{model.target_latency_ms ? `${model.target_latency_ms.toLocaleString()} ms` : "—"}</dd></div>
                    </dl>
                    <div className="application-model-capability-block">
                      <span>{t("applications.capabilities")}</span>
                      {capabilities.length > 0
                        ? <div className="application-model-capabilities">{capabilities.map((capability) => <span key={capability.label} title={capability.description}>{capability.label}</span>)}</div>
                        : <small>{t("applications.notSpecified")}</small>}
                    </div>
                  </article>;
                })}
              </div>
            </section>
          ))}
        </div>
      </section>
    </div>,
    document.querySelector(".gateway-shell") ?? document.body,
  );
}

export function ApplicationsScene({
  permissions,
}: {
  permissions: readonly string[];
}) {
  const { t } = useLanguage();
  const [items, setItems] = useState<ApplicationSummary[]>([]);
  const [selectedApplication, setSelectedApplication] = useState<ApplicationSummary | null>(null);
  const [deletingApplication, setDeletingApplication] = useState<ApplicationSummary | null>(null);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [environment, setEnvironment] = useState("");
  const [status, setStatus] = useState("");
  const [filters, setFilters] = useState<FilterCondition[]>([]);
  const [sortCriteria, setSortCriteria] = useState<SortCriterion<ApplicationSortField>[]>([]);
  const [page, setPage] = useState(1);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const pageSize = TABLE_PAGE_SIZE;
  const [total, setTotal] = useState(0);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const canManage = permissions.includes("applications.manage_own");
  const canCreate = canManage;
  const scope = permissions.includes("applications.read_global") ? "global" : "own";

  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => void listApplications({
        scope,
        search: search || undefined,
        environment: (environment || undefined) as ApplicationEnvironment | undefined,
        status: (status || undefined) as "active" | "paused" | "archived" | undefined,
        ...sortQueryParams(sortCriteria),
        page,
        page_size: pageSize,
      })
      .then((response) => {
        if (!active) return;
        setItems(response.items);
        setTotal(response.total);
        setState("ready");
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setState("error");
        setError(apiMessage(reason, t("applications.error")));
      }), 300);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [environment, page, pageSize, refreshVersion, scope, search, sortCriteria, status, t]);

  function updateFilters(next: FilterCondition[]) {
    setFilters(next);
    const name = next.find((item) => item.field === "name" && item.value.trim());
    const environment = next.find((item) => item.field === "environment" && item.value);
    const status = next.find((item) => item.field === "status" && item.value);
    setSearch(name?.value.trim() ?? "");
    setEnvironment(environment?.value ?? "");
    setStatus(status?.value ?? "");
    setPage(1);
  }

  function toggleSort(field: ApplicationSortField) {
    setSortCriteria((current) => toggleSortCriterion(current, field));
    setPage(1);
  }

  const environmentLabel = (value: string) => value === "production" ? t("applications.production") : value === "staging" ? t("applications.staging") : value === "development" ? t("applications.development") : value;
  const objectiveLabel = (value: string) => value === "Balanced" ? t("applications.balanced") : value === "Lowest cost" ? t("applications.lowestCost") : value === "Lowest latency" ? t("applications.lowestLatency") : value === "Highest quality" ? t("applications.highestQuality") : value;
  const statusLabel = (value: string) => value === "active" ? t("applications.active") : value === "paused" ? t("applications.paused") : value === "archived" ? t("applications.archived") : value;
  const budgetLabel = (value: string) => value === "Not set" ? t("applications.notSet") : value;
  const sortLabels: Record<ApplicationSortField, string> = { name: t("requests.application"), created_at: t("applications.created"), updated_at: t("applications.updated"), environment: t("applications.environment"), status: t("applications.status") };

  if (state === "loading") return <ListPageSkeleton label={t("applications.loading")} filters={3} columns={canManage ? 6 : 5} />;

  async function confirmApplicationDeletion() {
    if (!deletingApplication || deletePending) return;
    setDeletePending(true);
    setDeleteError(null);
    try {
      await deleteApplication(deletingApplication.id);
      setTotal((current) => Math.max(0, current - 1));
      if (items.length === 1 && page > 1) setPage((current) => Math.max(1, current - 1));
      else setRefreshVersion((current) => current + 1);
      setState("loading");
      setDeletingApplication(null);
    } catch (reason: unknown) {
      setDeleteError(apiMessage(reason, t("applications.error")));
    } finally {
      setDeletePending(false);
    }
  }

  return (
    <>
      <section className="gateway-page-heading">
        <div>
          <p className="gateway-eyebrow">{t("applications.workspace")}</p>
          <h1>{t("applications.pageTitle")}</h1>
          <p>{t("applications.description")}</p>
        </div>
        {canCreate && (
          <Link href="/applications/new" className="gateway-button">
            {t("applications.new")}
          </Link>
        )}
      </section>
      <FilterBuilder fields={[{ key: "name", label: t("applications.name"), type: "text" }, { key: "environment", label: t("applications.environment"), type: "enum", options: [{ value: "production", label: t("applications.production") }, { value: "staging", label: t("applications.staging") }, { value: "development", label: t("applications.development") }] }, { key: "status", label: t("applications.status"), type: "enum", options: [{ value: "active", label: t("applications.active") }, { value: "paused", label: t("applications.paused") }, { value: "archived", label: t("applications.archived") }] }]} value={filters} onChange={updateFilters} />
      {state === "error" && <SceneState message={error ?? t("applications.error")} tone="error" />}
      {state === "ready" && items.length === 0 && (
        <SceneState message={t("applications.noMatch")} />
      )}
      {items.length > 0 && (
        <section className="gateway-panel table-panel">
          <SortPrioritySummary criteria={sortCriteria} labels={sortLabels} />
          <div className="gateway-table-wrap">
            <table className={`gateway-table applications-table${canManage ? " applications-table--manageable" : ""}`}>
              <thead>
                <tr>
                  <SortableTableHeader field="name" label={t("requests.application")} criteria={sortCriteria} onToggle={toggleSort} />
                  <th>{t("applications.routingObjective")}</th>
                  <th>{t("applications.modelCatalog")}</th>
                  <th className="numeric">{t("applications.budget")}</th>
                  <SortableTableHeader field="status" label={t("applications.status")} criteria={sortCriteria} onToggle={toggleSort} />
                  {canManage && <th className="application-actions-column" aria-label={t("applications.action")}>{t("applications.action")}</th>}
                </tr>
              </thead>
              <tbody>
                {items.map((application) => {
                  const item = toApplicationViewModel(application);
                  return (
                    <tr key={item.id}>
                      <td>
                        <Link className="application-identity" href={"/applications/" + item.id}>
                          <span className="application-monogram" aria-hidden="true">{item.name.trim().split(/\s+/).slice(0, 2).map((word) => word[0]).join("").toLocaleUpperCase("vi-VN")}</span>
                          <span className="application-identity-copy">
                            <strong>{item.name}</strong>
                            <small>{item.slug}</small>
                          </span>
                        </Link>
                        <span className={`application-environment application-environment--${item.environment}`}>{environmentLabel(item.environment)}</span>
                      </td>
                      <td><span className="application-objective"><i aria-hidden="true" />{objectiveLabel(item.objective)}</span></td>
                      <td>
                        <div className="application-models-cell">
                          <div className="application-models-summary" aria-label={`${item.modelTotal} ${item.modelTotal === 1 ? "model" : "models"} configured`}>
                            <strong>{item.modelTotal}</strong>
                            <span>{t("applications.modelsConfigured", { count: item.modelTotal })}</span>
                          </div>
                          {item.models.length > 0 ? (
                            <div className="application-model-preview" aria-label={`${item.modelTotal} available models`}>
                              {item.models.slice(0, 2).map((model) => <span key={model} title={model}>{model}</span>)}
                              {item.modelTotal > item.models.length && (
                                <button className="application-model-more" type="button" onClick={() => setSelectedApplication(application)} aria-label={t("applications.moreModels", { count: item.modelTotal - item.models.length })}>
                                  {t("applications.moreModels", { count: item.modelTotal - item.models.length })}
                                </button>
                              )}
                              {item.modelTotal <= item.models.length && (
                                <button className="application-model-more application-model-details" type="button" onClick={() => setSelectedApplication(application)}>
                                  {t("applications.view")}
                                </button>
                              )}
                            </div>
                          ) : (
                            <span className="application-model-empty">{t("applications.managedByGateway")}</span>
                          )}
                        </div>
                      </td>
                      <td className="numeric"><span className="application-budget-value">{budgetLabel(item.budget)}</span></td>
                      <td className="application-status-cell">
                        <span className={`gateway-status gateway-status--${applicationStatusTone(item.status)}`}>
                          <i className="status-pin" aria-hidden="true" />{statusLabel(item.status)}
                        </span>
                      </td>
                      {canManage && <td className="gateway-table-actions-cell application-actions-column">
                        {application.is_owned_by_current_user && <button
                          className="gateway-icon-button gateway-icon-button--danger"
                          type="button"
                          aria-label={t("applications.delete", { name: item.name })}
                          title={t("applications.delete", { name: item.name })}
                          onClick={() => {
                            setDeleteError(null);
                            setDeletingApplication(application);
                          }}
                        >
                          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                            <path d="M4 7h16M10 11v6m4-6v6M6 7l1 14h10l1-14M9 7V4h6v3" />
                          </svg>
                        </button>}
                      </td>}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <PaginationControls page={page} pageSize={pageSize} total={total} onPageChange={setPage} label={t("applications.pagination")} />
        </section>
      )}
      {selectedApplication && <ApplicationModelsDialog application={selectedApplication} onClose={() => setSelectedApplication(null)} />}
      {deletingApplication && <DeleteConfirmDialog
        resourceType={t("applications.application")}
        resourceName={deletingApplication.name}
        description={t("applications.deleteDescription")}
        pending={deletePending}
        error={deleteError}
        onClose={() => {
          if (deletePending) return;
          setDeletingApplication(null);
          setDeleteError(null);
        }}
        onConfirm={() => void confirmApplicationDeletion()}
      />}
    </>
  );
}

export function NewApplicationScene({ permissions = [] }: { permissions?: readonly string[] }) {
  const { t } = useLanguage();
  const router = useRouter();
  const canManageKeys = permissions.includes("applications.write_own") || permissions.includes("applications.manage_own");
  const [createKeyNow, setCreateKeyNow] = useState(canManageKeys);
  const [keyName, setKeyName] = useState("");
  const [createdApplication, setCreatedApplication] = useState<ApplicationDetail | null>(null);
  const [rawKey, setRawKey] = useState<string | null>(null);
  const submitting = useRef(false);
  const [name, setName] = useState("");
  const [environment, setEnvironment] = useState<ApplicationEnvironment>("development");
  const [budget, setBudget] = useState("250");
  const [modelPools, setModelPools] = useState<ModelPool[]>([]);
  const [modelPoolId, setModelPoolId] = useState("");
  const [poolsLoading, setPoolsLoading] = useState(() => permissions.includes("model_pools.read_own"));
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const canReadPools = permissions.includes("model_pools.read_own");
  const selectedPool = modelPools.find((pool) => pool.id === modelPoolId);
  const parsedBudget = Number(budget);
  const budgetPreview = budget && Number.isFinite(parsedBudget)
    ? new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(parsedBudget)
    : t("applications.notSet");

  useEffect(() => {
    if (!canReadPools) return;
    void listModelPools().then((pools) => {
      setModelPools(pools.filter((pool) => pool.status === "active"));
    }).catch(() => {
      setModelPools([]);
    }).finally(() => setPoolsLoading(false));
  }, [canReadPools]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current || rawKey) return;
    if (canManageKeys && createKeyNow && !keyName.trim()) {
      setError(t("applications.initialKeyNameRequired"));
      setState("error");
      return;
    }
    submitting.current = true;
    setState("saving");
    setError(null);
    try {
      const result = await completeApplicationSetup({
        slug,
        name: name.trim(),
        environment,
        gateway_config: { budget_ceiling: Number(budget), model_pool_id: modelPoolId || null },
      }, canManageKeys && createKeyNow ? keyName.trim() : null, createdApplication);
      if (canManageKeys && createKeyNow) {
        setCreatedApplication(result.application);
        if (result.key) {
          setRawKey(result.key.raw_key);
          setState("idle");
        } else {
          setError(apiMessage(result.keyError, t("keys.actionError")));
          setState("error");
        }
      } else {
        router.push("/applications/" + result.application.id);
      }
    } catch (reason) {
      setState("error");
      setError(apiMessage(reason, t("applications.createError")));
    } finally {
      submitting.current = false;
    }
  }

  if (createdApplication) return <>
    <section className="gateway-page-heading new-application-heading">
      <div><p className="gateway-eyebrow">{t("applications.new")}</p><h1>{t("applications.setupCreated", { name: createdApplication.name })}</h1><p>{t(rawKey ? "applications.setupReady" : "applications.setupKeyPending")}</p></div>
    </section>
    <section className="gateway-panel application-setup-result">
      {rawKey ? <OneTimeApiKeySecret rawKey={rawKey} /> : <form onSubmit={submit} aria-busy={state === "saving"}>
        <div className="notice notice--warning" role="alert"><strong>{t("applications.setupKeyPending")}</strong>{error && <p>{error}</p>}</div>
        <label className="field"><span>{t("applications.initialKeyName")}</span><input required value={keyName} maxLength={100} placeholder={t("applications.initialKeyNamePlaceholder")} disabled={state === "saving"} onChange={(event) => setKeyName(event.target.value)} /><small>{t("applications.initialKeyNameHint")}</small></label>
        <button type="submit" className="gateway-button" disabled={state === "saving" || !keyName.trim()}>{t(state === "saving" ? "common.saving" : "applications.retryKey")}</button>
      </form>}
      <div className="form-actions">
        {rawKey && <Link href="/sdk-guide" className="gateway-button gateway-button--ghost">{t("applications.stepConnect")}</Link>}
        <Link href={`/applications/${createdApplication.id}`} className="gateway-button gateway-button--secondary" aria-disabled={state === "saving"} onClick={(event) => { if (submitting.current) event.preventDefault(); }}>{t(rawKey ? "applications.openCreated" : "applications.continueWithoutKey")}</Link>
      </div>
    </section>
  </>;

  return (
    <>
      <section className="gateway-page-heading new-application-heading">
        <div>
          <p className="gateway-eyebrow">{t("applications.new")}</p>
          <h1>{t("applications.connectTitle")}</h1>
          <p>{t("applications.connectDescription")}</p>
        </div>
      </section>
      {state === "error" && <SceneState message={error ?? t("applications.createError")} tone="error" />}
      <div className="new-application-layout">
        <form className="gateway-panel form-panel new-application-panel" onSubmit={submit} aria-busy={state === "saving"}>
          <header className="new-application-panel-header">
            <span className="new-application-panel-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M7 9h10M7 13h6" /><circle cx="17" cy="15" r="2" /></svg>
            </span>
            <div>
              <p>{t("applications.configuration")}</p>
              <h2>{t("applications.details")}</h2>
              <small>{t("applications.detailsHint")}</small>
            </div>
            <span className="new-application-required"><i aria-hidden="true" /> {t("applications.required")}</span>
          </header>
          <fieldset className="new-application-fields" disabled={state === "saving"}>
          <div className="form-grid">
            <label className="field">
              <span>{t("applications.nameRequired")}</span>
              <input
                required
                minLength={3}
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder={t("applications.namePlaceholder")}
                autoFocus
              />
              <small>{t("applications.nameHint")}</small>
            </label>
            <label className="field">
              <span>{t("applications.slug")}</span>
              <input
                value={slug}
                readOnly
                aria-readonly="true"
                placeholder={t("applications.slugPlaceholder")}
              />
              <small>{t("applications.slugHint")}</small>
            </label>
            <label className="field">
              <span>{t("applications.environment")}</span>
              <select
                value={environment}
                onChange={(event) => setEnvironment(event.target.value as ApplicationEnvironment)}
              >
                <option value="development">{t("applications.development")}</option>
                <option value="staging">{t("applications.staging")}</option>
                <option value="production">{t("applications.production")}</option>
              </select>
              <small>{t("applications.environmentHint")}</small>
            </label>
            <label className="field">
              <span>{t("applications.monthlyBudgetUsd")}</span>
              <input
                type="number"
                min="0"
                step="0.01"
                value={budget}
                onChange={(event) => setBudget(event.target.value)}
                placeholder="250.00"
              />
              <small>{t("applications.budgetHint")}</small>
            </label>
            {canReadPools && (
              <ModelPoolField
                pools={modelPools}
                value={modelPoolId}
                onChange={setModelPoolId}
                loading={poolsLoading}
                className="field full"
              />
            )}
          </div>
          {canManageKeys && <section className="new-application-key-section" aria-labelledby="initial-api-key-title">
            <div className="new-application-key-heading"><h3 id="initial-api-key-title">{t("keys.title")}</h3><span>{t("applications.initialKeyOptional")}</span></div>
            <label className="new-application-key-choice">
              <input type="checkbox" checked={createKeyNow} onChange={(event) => setCreateKeyNow(event.target.checked)} aria-controls="initial-api-key-options" />
              <span><strong>{t("applications.createKeyNow")}</strong><small>{t("applications.createKeyNowHint")}</small></span>
            </label>
            {createKeyNow ? <div id="initial-api-key-options"><label className="field"><span>{t("applications.initialKeyName")}</span><input required value={keyName} maxLength={100} onChange={(event) => setKeyName(event.target.value)} placeholder={t("applications.initialKeyNamePlaceholder")} /><small>{t("applications.initialKeyNameHint")}</small></label><p className="new-application-key-note">{t("applications.initialKeyGenerated")}</p></div> : <p id="initial-api-key-options" className="new-application-key-note">{t("applications.keyLater")}</p>}
          </section>}
          </fieldset>
          <div className="form-actions new-application-actions">
            <span className="new-application-save-note">{t("applications.saveLater")}</span>
            <Link href="/applications" className="gateway-button gateway-button--ghost" aria-disabled={state === "saving"} onClick={(event) => { if (submitting.current) event.preventDefault(); }}>{t("common.cancel")}</Link>
            <button className="gateway-button new-application-submit" type="submit" disabled={state === "saving" || slug.length < 3 || (canManageKeys && createKeyNow && !keyName.trim())}>
              {state === "saving" ? t("applications.creating") : t(canManageKeys && createKeyNow ? "applications.createWithKey" : "applications.create")}
              <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h12m-4-4 4 4-4 4" /></svg>
            </button>
          </div>
        </form>

        <aside className="gateway-panel new-application-preview" aria-labelledby="application-preview-title">
          <div className="new-application-preview-heading">
            <div>
              <p className="gateway-eyebrow">{t("applications.livePreview")}</p>
              <h2 id="application-preview-title">{t("applications.yourApplication")}</h2>
            </div>
            <span>{t("applications.draft")}</span>
          </div>
          <div className="new-application-preview-identity" aria-live="polite">
            <span aria-hidden="true">{name.trim().charAt(0).toUpperCase() || "A"}</span>
            <div>
              <strong>{name.trim() || t("applications.untitled")}</strong>
              <small>{slug || "your-application-slug"}</small>
            </div>
          </div>
          <dl className="new-application-preview-list">
            {canManageKeys && <div><dt>{t("keys.title")}</dt><dd>{t(createKeyNow ? "applications.keyIncluded" : "applications.keySkipped")}</dd></div>}
            <div>
              <dt>{t("applications.environment")}</dt>
              <dd><i className={`environment-dot environment-dot--${environment}`} aria-hidden="true" />{t(`applications.${environment}` as "applications.development" | "applications.staging" | "applications.production")}</dd>
            </div>
            <div>
              <dt>{t("applications.monthlyBudget")}</dt>
              <dd>{budgetPreview}</dd>
            </div>
            {canReadPools && (
              <div>
                <dt>{t("applications.routingPool")}</dt>
                <dd>{selectedPool?.name ?? t("applications.systemDefault")}</dd>
              </div>
            )}
          </dl>
          <div className="new-application-next-steps">
            <strong>{t("applications.nextSteps")}</strong>
            <ol>
              <li><span>1</span><p><b>{t("applications.stepCreated")}</b><small>{t("applications.stepCreatedHint")}</small></p></li>
              <li><span>2</span><p><b>{t(canManageKeys && createKeyNow ? "applications.copyInitialKey" : "applications.stepKey")}</b><small>{t(canManageKeys && createKeyNow ? "applications.copyInitialKeyHint" : "applications.stepKeyHint")}</small></p></li>
              <li><span>3</span><p><b>{t("applications.stepConnect")}</b><small>{t("applications.stepConnectHint")}</small></p></li>
            </ol>
          </div>
        </aside>
      </div>
    </>
  );
}

export function ApplicationDetailScene({
  id,
  permissions,
}: {
  id?: string;
  permissions: readonly string[];
}) {
  const { t } = useLanguage();
  const [application, setApplication] = useState<ApplicationSummary | null>(null);
  const [modelCatalog, setModelCatalog] = useState<ModelSummary[]>([]);
  const [recentRequests, setRecentRequests] = useState<GatewayRequestSummary[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const canEdit = permissions.includes("applications.manage_own");
  const canReadRequests = permissions.some((permission) => ["requests.read_own", "requests.read_global", "usage.read_own", "usage.read_global"].includes(permission));

  useEffect(() => {
    if (!id) return;
    let active = true;
    void Promise.all([
      getApplication(id),
      listAvailableModels({ status: "active", page_size: 100 }),
      canReadRequests ? listGatewayRequests({ application_id: id, page: 1, page_size: 5 }) : Promise.resolve(null),
    ])
      .then(([value, models, requests]) => {
        if (!active) return;
        setApplication(value);
        setModelCatalog(models.items);
        setRecentRequests(requests?.items ?? []);
        setState("ready");

      })
      .catch((reason: unknown) => {
        if (!active) return;
        setState("error");
        setError(apiMessage(reason, t("applications.error")));
      });
    return () => {
      active = false;
    };
  }, [canReadRequests, id, t]);

  if (state === "loading") return <DetailSkeleton label={t("applications.pageTitle")} />;
  if (state === "error" || !application) {
    return <SceneState message={error ?? t("applications.noMatch")} tone="error" />;
  }
  const item = toApplicationViewModel(application, modelCatalog);
  const environmentLabel = item.environment === "production" ? t("applications.production") : item.environment === "staging" ? t("applications.staging") : item.environment === "development" ? t("applications.development") : item.environment;
  const objectiveLabel = item.objective === "Balanced" ? t("applications.balanced") : item.objective === "Lowest cost" ? t("applications.lowestCost") : item.objective === "Lowest latency" ? t("applications.lowestLatency") : item.objective === "Highest quality" ? t("applications.highestQuality") : item.objective;
  const budgetLabel = item.budget === "Not set" ? t("applications.notSet") : item.budget;
  const budget = item.budgetSummary;
  const budgetPercent = Math.min(100, Math.max(0, (budget?.usage_percent ?? 0) * 100));
  return (
    <>
      <section className="gateway-page-heading">
        <div>
          <p className="gateway-eyebrow">{t("applications.environmentApplication", { environment: environmentLabel })}</p>
          <h1>{item.name}</h1>
          <p>{item.slug} · {t("applications.configurationVersion", { version: item.configVersion ?? t("applications.notConfigured") })}</p>
        </div>
        {canEdit && (
          <Link href={"/applications/" + item.id + "/settings"} className="gateway-button gateway-button--secondary">
            {t("applications.configuration")}
          </Link>
        )}
      </section>
      <nav className="application-tabs" aria-label={t("applications.configuration")}>
        <Link className="is-active" href={"/applications/" + item.id}>{t("nav.overview")}</Link>
        <Link href={"/applications/" + item.id + "/playground"}>{t("requests.playground")}</Link>
        {canEdit && <Link href={"/applications/" + item.id + "/settings"}>{t("applications.settings")}</Link>}
      </nav>
      <section className="gateway-policy-grid">
        <article><span>{t("applications.routingObjective")}</span><strong>{objectiveLabel}</strong><small>{t("applications.serverConfiguration")}</small></article>
        <article><span>{t("applications.availableModels")}</span><strong>{t("applications.enabledModels", { count: item.models.length || t("applications.configured") })}</strong><small>{item.models.join(" · ")}</small></article>
        <article><span>{t("applications.budgetGuardrail")}</span><strong>{budgetLabel}</strong><small>{t("applications.persistedSupabase")}</small></article>
        <article><span>{t("nav.requests")}</span><strong>{item.requestCount}</strong><small>{t("applications.ownedUsageRecords")}</small></article>
        <article><span>Routing objective</span><strong>{item.objective}</strong><small>Server configuration</small></article>
        <article><span>Available models</span><strong>{item.models.length || "Configured"} enabled</strong><small>{item.models.join(" · ")}</small></article>
        <article className="application-budget-card">
          <span>Current-month budget</span>
          <strong>{budgetMoney(budget?.limit_usd ?? (item.budget === "Not set" ? null : Number(item.budget)))}</strong>
          <small>{budget ? `${budgetMoney(budget.spent_usd)} spent · ${budgetMoney(budget.remaining_usd)}` : "No monthly limit configured"}</small>
          {budget?.limit_usd !== null && budget?.limit_usd !== undefined && <>
            <div className="application-budget-meter" role="progressbar" aria-label="Monthly budget usage" aria-valuemin={0} aria-valuemax={100} aria-valuenow={budgetPercent}>
              <i style={{ width: `${budgetPercent}%` }} />
            </div>
            <em className={`application-budget-state application-budget-state--${budget.state}`}>{budgetStateLabel(budget.state)}</em>
          </>}
        </article>
        <article><span>Requests</span><strong>{item.requestCount}</strong><small>Usage records owned by this application</small></article>
      </section>
        {canReadRequests && <section className="gateway-panel application-recent-requests" aria-labelledby="recent-requests-title">
        <div className="gateway-section-heading"><div><h2 id="recent-requests-title">{t("applications.recentRequests")}</h2><p>{t("requests.description")}</p></div><Link href={`/requests?search=${encodeURIComponent(item.name)}`}>{t("applications.viewAllRequests")}</Link></div>
        {recentRequests.length === 0 ? <SceneState message={t("applications.noRecentRequests")} /> : <div className="application-recent-request-list">{recentRequests.map((request) => <Link key={request.request_id} href={`/requests/${encodeURIComponent(request.request_id)}`}><span><strong>{request.request_id}</strong><small>{request.source} / {new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(request.started_at))}</small></span><span className={`gateway-status gateway-status--${request.status === "completed" ? "healthy" : request.status === "failed" ? "incident" : "watch"}`}><i className="status-pin" aria-hidden="true" />{request.status === "completed" ? t("usage.completed") : request.status === "partial" ? t("usage.partial") : request.status === "failed" ? t("usage.failed") : request.status}</span><span className="application-recent-request-metrics">{request.total_latency_ms.toFixed(0)}ms / {request.total_cost.toFixed(4)}</span></Link>)}</div>}
      </section>}
    </>
  );
}

export function ApplicationSettingsScene({ id, permissions = [] }: { id?: string; permissions?: readonly string[] }) {
  const { t } = useLanguage();
  const router = useRouter();
  const [config, setConfig] = useState<GatewayConfig | null>(null);
  const [budget, setBudget] = useState("");
  const [warningThreshold, setWarningThreshold] = useState("0.8");
  const [objective, setObjective] = useState("balanced");
  const [maxLatency, setMaxLatency] = useState("");
  const [timeout, setTimeoutValue] = useState("30");
  const [retryLimit, setRetryLimit] = useState("2");
  const [modelPools, setModelPools] = useState<ModelPool[]>([]);
  const [modelPoolId, setModelPoolId] = useState("");
  const [poolsLoading, setPoolsLoading] = useState(() => permissions.includes("model_pools.read_own"));
  const [state, setState] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [message, setMessage] = useState<string | null>(null);
  const canManageKeys = permissions.includes("applications.write_own") || permissions.includes("applications.manage_own");
  const canReadPools = permissions.includes("model_pools.read_own");

  useEffect(() => {
    if (!id) return;
    let active = true;
    void getGatewayConfig(id)
      .then((value) => {
        if (!active) return;
        setConfig(value);
        setModelPoolId(value.model_pool_id ?? "");
        setBudget(value.budget_ceiling?.toString() ?? "");
        setWarningThreshold(value.warning_threshold.toString());
        setObjective(value.routing_objective);
        setMaxLatency(value.max_latency_ms?.toString() ?? "");
        setTimeoutValue(value.timeout_seconds.toString());
        setRetryLimit(value.retry_limit.toString());
        setState("ready");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setMessage(apiMessage(reason, t("applications.error")));
        setState("error");
      });
    return () => {
      active = false;
    };
  }, [id, t]);

  useEffect(() => {
    if (!canReadPools) return;
    void listModelPools().then((pools) => {
      setModelPools(pools.filter((pool) => pool.status === "active"));
    }).catch(() => {
      setModelPools([]);
    }).finally(() => setPoolsLoading(false));
  }, [canReadPools]);

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!id || !config) return;
    setState("saving");
    setMessage(null);
    try {
      const value = await updateGatewayConfig(id, {
        expected_version: config.version,
        model_pool_id: modelPoolId || null,
        budget_ceiling: budget ? Number(budget) : null,
        warning_threshold: Number(warningThreshold),
        routing_objective: objective,
        max_latency_ms: maxLatency ? Number(maxLatency) : null,
        timeout_seconds: Number(timeout),
        retry_limit: Number(retryLimit),
      });
      setConfig(value);
      setState("ready");
      setMessage(t("applications.configurationSaved"));
      router.refresh();
    } catch (reason) {
      setState("error");
      setMessage(apiMessage(reason, t("applications.error")));
    }
  }

  if (state === "loading") return <FormPageSkeleton label={t("applications.configuration")} fields={6} />;
  return (
    <>
      <section className="gateway-page-heading">
        <div>
          <p className="gateway-eyebrow">{t("applications.configuration")} · v{config?.version ?? "?"}</p>
          <h1>{t("applications.settings")}</h1>
          <p>{t("applications.settingsDescription")}</p>
        </div>
        {id && <Link href={"/applications/" + id} className="gateway-button gateway-button--ghost">{t("applications.back")}</Link>}
      </section>
      {id && <nav className="application-tabs" aria-label={t("applications.sections")}>
        <Link href={`/applications/${id}`}>{t("nav.overview")}</Link>
        <Link href={`/applications/${id}/playground`}>{t("requests.playground")}</Link>
        <Link className="is-active" href={`/applications/${id}/settings`}>{t("applications.settings")}</Link>
      </nav>}
      {message && <SceneState message={message} tone={state === "error" ? "error" : "neutral"} />}
      {config && (
        <form className="gateway-panel form-panel" onSubmit={save}>
          <div className="form-grid">
            <label className="field">
              <span>{t("applications.routingObjective")}</span>
              <select value={objective} onChange={(event) => setObjective(event.target.value)}>
                <option value="balanced">{t("applications.balanced")}</option>
                <option value="cost_first">{t("applications.lowestCost")}</option>
                <option value="latency_first">{t("applications.lowestLatency")}</option>
                <option value="quality_first">{t("applications.highestQuality")}</option>
              </select>
            </label>
            <label className="field">
              <span>{t("applications.monthlyBudget")}</span>
              <span>Budget limit per calendar month (USD)</span>
              <input type="number" min="0" step="0.01" value={budget} onChange={(event) => setBudget(event.target.value)} />
              <small>Usage is measured from persisted gateway request cost in the current UTC month.</small>
            </label>
            <label className="field">
              <span>Warning threshold</span>
              <input type="number" min="0" max="1" step="0.01" value={warningThreshold} onChange={(event) => setWarningThreshold(event.target.value)} />
              <small>Use 0.8 for an alert at 80% of the monthly budget.</small>
            </label>
            <label className="field">
              <span>{t("applications.maxLatency")}</span>
              <input type="number" min="1" value={maxLatency} onChange={(event) => setMaxLatency(event.target.value)} placeholder={t("applications.noLimit")} />
            </label>
            <label className="field">
              <span>{t("applications.timeoutSeconds")}</span>
              <input type="number" min="1" value={timeout} onChange={(event) => setTimeoutValue(event.target.value)} />
            </label>
            <label className="field">
              <span>{t("applications.retryLimit")}</span>
              <input type="number" min="0" max="10" value={retryLimit} onChange={(event) => setRetryLimit(event.target.value)} />
            </label>
            {canReadPools && (
              <ModelPoolField
                pools={modelPools}
                value={modelPoolId}
                onChange={setModelPoolId}
                loading={poolsLoading}
                className="field full"
              />
            )}
          </div>
          <div className="form-actions">
            <span className="save-hint">{t("applications.versionConflictHint", { version: config.version })}</span>
            <button className="gateway-button" type="submit" disabled={state === "saving"}>
              {state === "saving" ? t("common.saving") : t("providers.saveChanges")}
            </button>
          </div>
        </form>
      )}
      {id && <ApplicationKeyPanel applicationId={id} canManage={canManageKeys} />} 
    </>
  );
}
