"use client";

import Link from "next/link";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLanguage } from "@/components/language-provider";

import {
  createModelPool,
  deleteModelPool,
  getModelPool,
  listAvailableModels,
  listModelPools,
  suggestPoolFallbacks,
  updateModelPool,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type { FallbackSuggestion, ModelPool, ModelPoolMemberRole, ModelSummary } from "@/lib/gateway-types";
import { FormPageSkeleton, ListPageSkeleton } from "@/components/gateway/gateway-page-skeletons";
import { DeleteConfirmDialog } from "@/components/gateway/delete-confirm-dialog";
import { PaginationControls, TABLE_PAGE_SIZE, useClientTablePagination } from "@/components/gateway/pagination-controls";

function apiMessage(error: unknown, fallback: string): string {
  return error && typeof error === "object" && "message" in error
    ? String((error as GatewayApiError).message)
    : fallback;
}

function Notice({ children, error = false, success = false }: { children: React.ReactNode; error?: boolean; success?: boolean }) {
  return <div className={`notice${error ? " notice--warning" : success ? " notice--success" : ""}`} role={error ? "alert" : "status"}>{children}</div>;
}

export function ModelPoolsScene({ permissions }: { permissions: readonly string[] }) {
  const { t, language } = useLanguage();
  const [pools, setPools] = useState<ModelPool[]>([]);
  const [deletingPool, setDeletingPool] = useState<ModelPool | null>(null);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const poolPagination = useClientTablePagination(pools);
  const [actionMenu, setActionMenu] = useState<{ pool: ModelPool; top: number; left: number } | null>(null);
  const actionMenuRef = useRef<HTMLDivElement>(null);
  const actionTriggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    void listModelPools().then((value) => {
      setPools(value);
      setState("ready");
    }).catch((reason: unknown) => {
      setError(apiMessage(reason, t("pools.actionError")));
      setState("error");
    });
  }, [t]);

  useEffect(() => {
    if (!actionMenu) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || actionMenuRef.current?.contains(target) || actionTriggerRef.current?.contains(target)) return;
      setActionMenu(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        actionTriggerRef.current?.focus();
        setActionMenu(null);
      }
    };
    const closeOnViewportChange = () => setActionMenu(null);
    document.addEventListener("pointerdown", closeOnOutsidePointer, true);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", closeOnViewportChange);
    window.addEventListener("scroll", closeOnViewportChange, true);
    actionMenuRef.current?.querySelector<HTMLAnchorElement | HTMLButtonElement>("a, button")?.focus();
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer, true);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", closeOnViewportChange);
      window.removeEventListener("scroll", closeOnViewportChange, true);
    };
  }, [actionMenu]);

  const canManage = permissions.includes("model_pools.manage_own");

  async function confirmPoolDeletion() {
    if (!deletingPool || deletePending) return;
    setDeletePending(true);
    setDeleteError(null);
    try {
      await deleteModelPool(deletingPool.id);
      setPools((current) => current.filter((pool) => pool.id !== deletingPool.id));
      setDeletingPool(null);
    } catch (reason: unknown) {
      setDeleteError(apiMessage(reason, t("pools.actionError")));
    } finally {
      setDeletePending(false);
    }
  }

  function openActionMenu(pool: ModelPool, trigger: HTMLButtonElement) {
    if (actionMenu?.pool.id === pool.id) {
      setActionMenu(null);
      return;
    }
    const rect = trigger.getBoundingClientRect();
    const menuWidth = Math.min(220, window.innerWidth - 24);
    const menuHeight = canManage && pool.owner_user_id !== null ? 128 : 82;
    const left = Math.max(12, Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 12));
    const top = rect.bottom + menuHeight + 12 <= window.innerHeight
      ? rect.bottom + 8
      : Math.max(12, rect.top - menuHeight - 8);
    actionTriggerRef.current = trigger;
    setActionMenu({ pool, top, left });
  }

  if (!permissions.includes("model_pools.read_own")) return <Notice error>{t("workspace.accessDenied")}</Notice>;
  if (state === "loading") return <ListPageSkeleton label={t("pools.loading")} filters={0} columns={6} />;

  return <>
    <section className="gateway-page-heading">
      <div><p className="gateway-eyebrow">{t("pools.catalogue")}</p><h1>{t("pools.title")}</h1><p>{t("pools.description")}</p></div>
      {canManage && <Link href="/model-pools/new" className="gateway-button">{t("pools.create")}</Link>}
    </section>
    {state === "error" && <Notice error>{error}</Notice>}
    {state === "ready" && pools.length === 0 && <section className="gateway-panel model-pool-empty"><h2>{t("pools.emptyTitle")}</h2><p>{canManage ? t("pools.emptyManage") : t("pools.emptyRead")}</p>{canManage && <Link href="/model-pools/new" className="gateway-button">{t("pools.create")}</Link>}</section>}
    {state === "ready" && pools.length > 0 && <section className="gateway-panel table-panel model-pools-panel">
      <div className="gateway-table-wrap">
        <table className="gateway-table model-pools-table">
          <thead><tr><th>{t("pools.pool")}</th><th>{t("pools.purpose")}</th><th>{t("pools.models")}</th><th>{t("pools.health")}</th><th>{t("pools.status")}</th><th className="model-pool-actions-heading">{t("pools.action")}</th></tr></thead>
          <tbody>{poolPagination.pageItems.map((pool) => {
            const primaryCount = pool.members.filter((member) => member.role === "primary").length;
            const fallbackCount = pool.members.filter((member) => member.role === "fallback").length;
            const warningCount = pool.warnings?.length ?? 0;
            const systemPool = pool.owner_user_id === null;
            return <tr key={pool.id}>
              <td>
                <div className="model-pool-identity">
                  <span className="model-pool-mark" aria-hidden="true">{pool.name.trim().charAt(0).toLocaleUpperCase("vi-VN")}</span>
                  <span className="model-pool-identity-copy"><strong>{pool.name}</strong><small>{systemPool ? t("pools.systemReadOnly") : t("pools.workspacePool")}</small></span>
                </div>
              </td>
              <td>
                <div className="model-pool-purpose">
                  <span className={
                    "model-pool-kind " +
                    (isTemplatePool(pool)
                      ? "model-pool-kind--template"
                      : systemPool
                        ? "model-pool-kind--default"
                        : "model-pool-kind--custom")
                  }>
                    {isTemplatePool(pool) ? t("pools.taskTemplate") : systemPool ? t("pools.systemDefault") : t("pools.customRouting")}
                  </span>
                  <span className="model-pool-purpose-copy">{poolPurposeLabel(pool, language)}</span>
                </div>
              </td>
              <td>
                <div className="model-pool-member-summary" aria-label={t("pools.memberSummary", { primary: primaryCount, fallback: fallbackCount })}>
                  <span className="model-pool-count model-pool-count--primary"><i aria-hidden="true" /><strong>{primaryCount}</strong> {t("pools.primary")}</span>
                  <span className="model-pool-count model-pool-count--fallback"><i aria-hidden="true" /><strong>{fallbackCount}</strong> {t("pools.fallback")}</span>
                </div>
              </td>
              <td>{warningCount > 0
                ? <span className="gateway-status gateway-status--watch" title={(pool.warnings ?? []).join(" · ")}><i className="status-pin" aria-hidden="true" />{t("pools.warningCount", { count: warningCount })}</span>
                : <span className="gateway-status gateway-status--healthy"><i className="status-pin" aria-hidden="true" />{t("pools.clear")}</span>}</td>
              <td><span className={`gateway-status gateway-status--${pool.status === "active" ? "healthy" : "watch"}`}><i className="status-pin" aria-hidden="true" />{pool.status === "active" ? t("models.active") : t("models.inactive")}</span></td>
              <td className="gateway-table-actions-cell model-pool-table-actions">
                <button
                  ref={actionMenu?.pool.id === pool.id ? actionTriggerRef : undefined}
                  type="button"
                  className="gateway-icon-button model-pool-actions-trigger"
                  aria-label={`${t("pools.action")}: ${pool.name}`}
                  aria-haspopup="menu"
                  aria-expanded={actionMenu?.pool.id === pool.id}
                  aria-controls={actionMenu?.pool.id === pool.id ? "model-pool-actions-popup" : undefined}
                  onClick={(event) => openActionMenu(pool, event.currentTarget)}
                >⋯</button>
              </td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      <PaginationControls page={poolPagination.page} pageSize={TABLE_PAGE_SIZE} total={pools.length} onPageChange={poolPagination.setPage} label={t("pools.pagination")} />
    </section>}
    {actionMenu && typeof document !== "undefined" && createPortal(<div id="model-pool-actions-popup" ref={actionMenuRef} className="provider-actions-popup model-pool-actions-popup" style={{ left: actionMenu.left, top: actionMenu.top }} role="menu" aria-label={`${t("pools.action")}: ${actionMenu.pool.name}`}>
      <p>{t("pools.action")}</p>
      {canManage && actionMenu.pool.owner_user_id !== null ? <>
        <Link role="menuitem" className="model-pool-menu-link" href={`/model-pools/${actionMenu.pool.id}`} onClick={() => setActionMenu(null)}>{t("pools.edit")}</Link>
        <button role="menuitem" type="button" className="provider-actions-danger" onClick={() => { setActionMenu(null); setDeleteError(null); setDeletingPool(actionMenu.pool); }}>{t("pools.delete")}</button>
      </> : <button role="menuitem" type="button" disabled>{actionMenu.pool.owner_user_id === null ? t("pools.systemManaged") : t("pools.readOnly")}</button>}
    </div>, document.body)}
    {deletingPool && <DeleteConfirmDialog
      resourceType={t("pools.pool")}
      resourceName={deletingPool.name}
      description={t("pools.deleteDescription")}
      pending={deletePending}
      error={deleteError}
      onClose={() => {
        if (deletePending) return;
        setDeletingPool(null);
        setDeleteError(null);
      }}
      onConfirm={() => void confirmPoolDeletion()}
    />}
  </>;
}

// Capability tiers come from the catalogue quality_tier labels (docs/routing-design-decisions.md section 5).
const TIERS = [
  { key: "simple", name: { vi: "Nhỏ", en: "Small" }, use: { vi: "Câu hỏi ngắn, tra cứu, viết lại, phân loại", en: "Short questions, lookups, rewriting, classification" } },
  { key: "mid", name: { vi: "Trung bình", en: "Mid" }, use: { vi: "Suy luận thường ngày, tóm tắt, code thông thường", en: "Everyday reasoning, summaries, ordinary code" } },
  { key: "hard", name: { vi: "Lớn", en: "Large" }, use: { vi: "Suy luận khó, toán học, nhiều bước và câu trả lời quan trọng", en: "Hard reasoning, maths, multi-step and high-stakes answers" } },
] as const;
type TierKey = (typeof TIERS)[number]["key"];
const tierName = (tier: string | undefined, language: "vi" | "en") => TIERS.find((item) => item.key === tier)?.name[language] ?? (language === "vi" ? "Chưa có tầng" : "No tier");

function money(value?: number | null): string {
  if (value === undefined || value === null) return "—";
  return `$${value < 1 ? Number(value.toFixed(3)).toString() : value.toFixed(2)}`;
}

function modelFacts(model: ModelSummary, language: "vi" | "en"): string {
  const context = model.context_window ? `${Math.round(model.context_window / 1000)}k ${language === "vi" ? "ngữ cảnh" : "context"}` : (language === "vi" ? "chưa rõ ngữ cảnh" : "context unknown");
  const caps = model.capabilities.filter((cap) => !["text", "chat"].includes(cap)).slice(0, 4).join(", ");
  return [
    `${tierName(model.quality_tier, language)} ${language === "vi" ? "tầng" : "tier"}`,
    `${money(model.pricing.input_per_million)} / ${money(model.pricing.output_per_million)} ${language === "vi" ? "mỗi 1 triệu token" : "per 1M tokens"}`,
    context,
    caps,
  ].filter(Boolean).join(" · ");
}

function isTemplatePool(pool: ModelPool): boolean {
  return pool.owner_user_id === null && pool.purpose.startsWith("system-template:");
}

function poolPurposeLabel(pool: ModelPool, language: "vi" | "en"): string {
  if (pool.owner_user_id !== null) return pool.purpose;
  if (!isTemplatePool(pool)) return language === "vi" ? "Định tuyến mặc định cho ứng dụng chưa chọn pool." : "Default routing for applications without a pool.";
  const task = pool.purpose.slice("system-template:".length).replace(/[-_]+/g, " ").trim();
  return task
    ? language === "vi" ? `Định tuyến yêu cầu ${task} đến model chuyên biệt cho tác vụ.` : `Routes ${task.replace(/\b\w/g, (letter) => letter.toUpperCase())} requests to a task-specific model.`
    : language === "vi" ? "Mẫu tác vụ hệ thống." : "System task template.";
}

// Warn, never block: a backwards price ladder, several providers, unhealthy models.
function ladderWarnings(primaries: ModelSummary[], language: "vi" | "en"): string[] {
  const notes: string[] = [];
  const ladder = TIERS
    .map((tier) => primaries.find((model) => model.quality_tier === tier.key))
    .filter((model): model is ModelSummary => Boolean(model));
  ladder.slice(0, -1).forEach((lower, index) => {
    const higher = ladder[index + 1];
    if ((lower.pricing.output_per_million ?? 0) > (higher.pricing.output_per_million ?? 0)) {
      notes.push(language === "vi" ? `${lower.display_name} (${tierName(lower.quality_tier, language)}) có giá cao hơn ${higher.display_name} (${tierName(higher.quality_tier, language)}).` : `${lower.display_name} (${tierName(lower.quality_tier, language)}) costs more than ${higher.display_name} (${tierName(higher.quality_tier, language)}).`);
    }
  });
  const providers = new Set(ladder.map((model) => model.provider));
  if (providers.size > 1) notes.push(language === "vi" ? `Các model theo tầng đến từ ${providers.size} nhà cung cấp; một thang model cùng họ sẽ dễ dự đoán hơn.` : `The tier models come from ${providers.size} providers; a ladder from one family behaves more predictably.`);
  const unhealthy = ladder.filter((model) => model.health_status === "unhealthy");
  if (unhealthy.length) notes.push(language === "vi" ? `Hiện không ổn định: ${unhealthy.map((model) => model.display_name).join(", ")}.` : `Unhealthy right now: ${unhealthy.map((model) => model.display_name).join(", ")}.`);
  return notes;
}

type EditableMember = { key: string; model_id: string; role: ModelPoolMemberRole };
type FallbackMode = "system" | "manual" | "none";

function ModelPicker({ models, selectedIds, onSelect, label = "Add a model" }: { models: ModelSummary[]; selectedIds: Set<string>; onSelect: (model: ModelSummary) => void; label?: string }) {
  const { t, language } = useLanguage();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number; width: number; maxListHeight: number } | null>(null);
  const inputId = useId();
  const listboxId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const results = useMemo(() => models.filter((model) => !selectedIds.has(model.id) && `${model.display_name} ${model.name} ${model.provider}`.toLowerCase().includes(query.trim().toLowerCase())), [models, query, selectedIds]);
  useEffect(() => {
    if (!open) return;
    const updatePosition = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const viewportPadding = 16;
      const popupChromeHeight = 82;
      const preferredListHeight = 252;
      const availableBelow = window.innerHeight - rect.bottom - viewportPadding;
      const availableAbove = rect.top - viewportPadding;
      const openBelow = availableBelow >= popupChromeHeight + 120 || availableBelow >= availableAbove;
      const availableForList = Math.max(120, Math.min(preferredListHeight, (openBelow ? availableBelow : availableAbove) - popupChromeHeight));
      const popupHeight = popupChromeHeight + availableForList;
      const width = Math.min(Math.max(rect.width, 320), window.innerWidth - viewportPadding * 2);
      setPosition({
        top: openBelow ? rect.bottom + 8 : Math.max(viewportPadding, rect.top - popupHeight - 8),
        left: Math.max(viewportPadding, Math.min(rect.left, window.innerWidth - width - viewportPadding)),
        width,
        maxListHeight: availableForList,
      });
    };
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !popoverRef.current?.contains(target)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") { setOpen(false); triggerRef.current?.focus(); } };
    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    inputRef.current?.focus();
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const popup = open && position && typeof document !== "undefined" ? createPortal(
    <div ref={popoverRef} className="model-picker-popover" style={{ top: position.top, left: position.left, width: position.width, ["--model-picker-list-height" as string]: `${position.maxListHeight}px` }}>
      <label htmlFor={inputId}>{t("pools.searchModel")}</label>
      <input ref={inputRef} id={inputId} type="search" role="combobox" aria-expanded={open} aria-controls={listboxId} aria-autocomplete="list" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("pools.searchPlaceholder")} autoComplete="off" />
      <div id={listboxId} className="model-picker-results" role="listbox" aria-label={t("pools.matchingModels")}>{results.length > 0 ? results.map((model) => <button type="button" role="option" aria-selected={false} key={model.id} onClick={() => { onSelect(model); setOpen(false); setQuery(""); triggerRef.current?.focus(); }}><strong>{model.display_name}</strong><span>{model.provider} · {model.name}</span><span>{modelFacts(model, language)}</span></button>) : <p>{t("pools.noMatchingModel")}</p>}</div>
    </div>,
    document.body,
  ) : null;
  return <div className="model-picker"><button ref={triggerRef} className="model-picker-trigger" type="button" aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? listboxId : undefined} onClick={() => setOpen((value) => !value)}>{label}</button>{popup}</div>;
}

export function ModelPoolEditorScene({ poolId, permissions }: { poolId?: string; permissions: readonly string[] }) {
  const { t, language } = useLanguage();
  const [savedPoolId, setSavedPoolId] = useState<string | undefined>(poolId);
  const editing = Boolean(savedPoolId);
  const [models, setModels] = useState<ModelSummary[]>([]);
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [status, setStatus] = useState<"active" | "inactive">("active");
  const [members, setMembers] = useState<EditableMember[]>([]);
  const [fallbackMode, setFallbackMode] = useState<FallbackMode>("system");
  const [allowOtherProviders, setAllowOtherProviders] = useState(true);
  const [suggestionResult, setSuggestionResult] = useState<{ key: string; items: FallbackSuggestion[] } | null>(null);
  const [serverWarnings, setServerWarnings] = useState<string[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  function loadPool(pool: ModelPool) {
    setMembers(pool.members.map((member) => ({ key: member.id, model_id: member.model_id, role: member.role ?? "primary" })));
    setServerWarnings(pool.warnings ?? []);
    if (pool.members.some((member) => member.role === "fallback")) setFallbackMode("manual");
  }

  useEffect(() => {
    let active = true;
    const modelRequest = (async () => {
      const first = await listAvailableModels({ status: "active", page_size: 100 });
      const pages = await Promise.all(Array.from(
        { length: Math.max(0, Math.ceil(first.total / 100) - 1) },
        (_, index) => listAvailableModels({ status: "active", page_size: 100, page: index + 2 }),
      ));
      return { ...first, items: [...first.items, ...pages.flatMap((page) => page.items)] };
    })();
    const poolRequest = poolId ? getModelPool(poolId) : Promise.resolve<ModelPool | null>(null);
    void Promise.all([modelRequest, poolRequest]).then(([modelPage, pool]) => {
      if (!active) return;
      setModels(modelPage.items);
      if (pool) {
        setName(pool.name);
        setPurpose(pool.purpose);
        setStatus(pool.status);
        loadPool(pool);
      }
      setState("ready");
    }).catch((reason: unknown) => {
      if (!active) return;
      setError(apiMessage(reason, t("pools.actionError")));
      setState("error");
    });
    return () => { active = false; };
  }, [poolId, t]);

  const modelById = useMemo(() => new Map(models.map((model) => [model.id, model])), [models]);
  const primaries = members.filter((member) => member.role === "primary");
  const fallbacks = members.filter((member) => member.role === "fallback");
  const primaryByTier = new Map(primaries.map((member) => [modelById.get(member.model_id)?.quality_tier, member]));
  const ladderComplete = primaries.length === 3 && TIERS.every((tier) => primaryByTier.has(tier.key));
  const primaryKey = primaries.map((member) => member.model_id).sort().join(",");

  // After the 3 tier models are chosen, show what the system would use as fallback.
  // Results are keyed by their inputs so an answer for an older choice is never shown.
  const suggestionKey = `${primaryKey}|${allowOtherProviders}`;
  const suggestions = suggestionResult?.key === suggestionKey ? suggestionResult.items : null;
  useEffect(() => {
    if (!ladderComplete || fallbackMode !== "system") return;
    let active = true;
    const [ids, allow] = suggestionKey.split("|");
    void suggestPoolFallbacks(ids.split(","), allow === "true")
      .then((items) => { if (active) setSuggestionResult({ key: suggestionKey, items }); })
      .catch((reason: unknown) => { if (active) setError(apiMessage(reason, t("pools.actionError"))); });
    return () => { active = false; };
  }, [ladderComplete, fallbackMode, suggestionKey, t]);

  function setPrimary(tier: TierKey, model: ModelSummary) {
    setMembers((current) => [
      ...current.filter((member) => member.model_id !== model.id && !(member.role === "primary" && modelById.get(member.model_id)?.quality_tier === tier)),
      { key: crypto.randomUUID(), model_id: model.id, role: "primary" },
    ]);
  }
  function addFallback(model: ModelSummary) {
    setMembers((current) => [...current, { key: crypto.randomUUID(), model_id: model.id, role: "fallback" }]);
  }
  function removeMember(key: string) {
    setMembers((current) => current.filter((member) => member.key !== key));
  }
  function membersToSave() {
    const base = primaries.map(({ model_id }) => ({ model_id, role: "primary" as const }));
    if (fallbackMode === "none") return base;
    const chosen = fallbackMode === "system"
      ? (suggestions ?? []).flatMap((item) => item.model_id ? [item.model_id] : [])
      : fallbacks.map((member) => member.model_id);
    return [...base, ...chosen.map((model_id) => ({ model_id, role: "fallback" as const }))];
  }
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setState("saving");
    setError(null);
    setSuccess(null);
    const input = { name: name.trim(), purpose: purpose.trim(), status, members: membersToSave() };
    try {
      const saved = savedPoolId ? await updateModelPool(savedPoolId, input) : await createModelPool(input);
      if (!savedPoolId) {
        setSavedPoolId(saved.id);
        window.history.replaceState(null, "", `/model-pools/${saved.id}`);
      }
      loadPool(saved);
      setSuccess(editing ? t("pools.updated") : t("pools.created"));
      setState("ready");
    } catch (reason) {
      setError(apiMessage(reason, t("pools.actionError")));
      setState("error");
    }
  }

  if (!permissions.includes("model_pools.manage_own")) return <Notice error>{t("pools.manageDenied")}</Notice>;
  if (state === "loading") return <FormPageSkeleton label={t("pools.loadingEditor")} fields={5} />;
  if (state === "error" && !models.length) return <Notice error>{error}</Notice>;
  const selectedIds = new Set(members.map((member) => member.model_id));
  const primaryModels = primaries.flatMap((member) => modelById.get(member.model_id) ?? []);
  const warnings = [...new Set([
    ...ladderWarnings(primaryModels, language),
    ...serverWarnings.filter((note) => note.startsWith("Created before") || note.startsWith("Cannot be called")),
  ])];
  const tiered = models.filter((model) => TIERS.some((tier) => tier.key === model.quality_tier));
  const priceDelta = (value: number | null) => value === null ? ""
    : value > 0 ? ` · +${money(value)} ${t("pools.perMillionOutput")}`
    : value < 0 ? ` · ${money(-value)} ${t("pools.cheaperPerMillionOutput")}` : ` · ${t("pools.samePrice")}`;
  const waitingForSuggestions = fallbackMode === "system" && ladderComplete && suggestions === null;
  return <>
    <section className="gateway-page-heading"><div><p className="gateway-eyebrow">{t("pools.catalogue")}</p><h1>{editing ? t("pools.edit") : t("pools.createTitle")}</h1><p>{t("pools.editorDescription")}</p></div><Link href="/model-pools" className="gateway-button gateway-button--ghost">{t("pools.back")}</Link></section>
    <Notice><span>{t("pools.callableModelsHint")} {permissions.includes("models.manage") && <><Link href="/providers">{t("providers.title")}</Link> · <Link href="/models">{t("models.title")}</Link></>}</span></Notice>
    {error && <Notice error>{error}</Notice>}{success && <Notice success>{success}</Notice>}
    <form className="gateway-panel form-panel" onSubmit={submit}>
      <section className="form-section"><div><h2>{t("pools.details")}</h2><p>{t("pools.detailsDescription")}</p></div><div className="form-grid"><label className="field"><span>{t("pools.nameRequired")}</span><input required value={name} onChange={(event) => setName(event.target.value)} maxLength={100} placeholder={t("pools.namePlaceholder")} /></label><label className="field"><span>{t("pools.purposeRequired")}</span><input required value={purpose} onChange={(event) => setPurpose(event.target.value)} maxLength={100} placeholder={t("pools.purposePlaceholder")} /></label><label className="field"><span>{t("pools.status")}</span><select value={status} onChange={(event) => setStatus(event.target.value as "active" | "inactive")}><option value="active">{t("models.active")}</option><option value="inactive">{t("models.inactive")}</option></select></label></div></section>
      <section className="form-section model-pool-members-section">
        <div><h2>{t("pools.modelsByTier")}</h2><p>{t("pools.modelsByTierDescription")}</p></div>
        <ul className="model-pool-members">{TIERS.map((tier) => {
          const member = primaryByTier.get(tier.key);
          const model = member ? modelById.get(member.model_id) : undefined;
          return <li key={tier.key}>
            <div><strong>{t("pools.tierLabel", { tier: tier.name[language] })}{model ? ` — ${model.display_name}` : ""}</strong><small>{t("pools.forUse", { use: tier.use[language] })}</small><small>{model ? `${model.provider} · ${modelFacts(model, language)}` : t("pools.noTierModel")}</small></div>
            <div className="member-actions">{member && <button type="button" onClick={() => removeMember(member.key)} aria-label={t("pools.removeModel", { model: model?.display_name ?? t("models.model") })}>{t("pools.remove")}</button>}<ModelPicker models={tiered.filter((item) => item.quality_tier === tier.key)} selectedIds={selectedIds} onSelect={(picked) => setPrimary(tier.key, picked)} label={model ? t("pools.change") : t("pools.choose")} /></div>
          </li>;
        })}</ul>
      </section>
      {warnings.length > 0 && <Notice error>{warnings.map((note) => <div key={note}>{note}</div>)}</Notice>}
      {ladderComplete && <section className="form-section">
        <div><h2>{t("pools.fallback")}</h2><p>{t("pools.fallbackDescription")}</p></div>
        <div className="model-pool-fallback-content">
        <div className="form-grid">
          <label className="field"><span>{t("pools.fallbackPicker")}</span><select value={fallbackMode} onChange={(event) => setFallbackMode(event.target.value as FallbackMode)}><option value="system">{t("pools.systemPick")}</option><option value="manual">{t("pools.manualPick")}</option><option value="none">{t("pools.noFallback")}</option></select></label>
          {fallbackMode === "system" && <label className="field"><span>{t("pools.allowOtherProvider")}</span><select value={allowOtherProviders ? "yes" : "no"} onChange={(event) => setAllowOtherProviders(event.target.value === "yes")}><option value="yes">{t("usage.yes")}</option><option value="no">{t("pools.sameProviderOnly")}</option></select></label>}
        </div>
        {fallbackMode === "system" && <ul className="model-pool-members">{suggestions === null ? <li><div><small>{t("pools.findingFallbacks")}</small></div></li> : suggestions.map((item) => {
          const model = item.model_id ? modelById.get(item.model_id) : undefined;
          return <li key={item.for_model_id}><div><strong>{t("pools.tierFallback", { tier: tierName(item.tier, language), model: model?.display_name ?? t("pools.nextTier") })}</strong><small>{model ? `${model.provider} · ${item.reason}${priceDelta(item.output_price_delta_per_million)}` : item.reason}</small></div></li>;
        })}</ul>}
        {fallbackMode === "manual" && <div>
          <ModelPicker models={tiered} selectedIds={selectedIds} onSelect={addFallback} label={t("pools.addFallback")} />
          <ul className="model-pool-members">{fallbacks.map((member) => {
            const model = modelById.get(member.model_id);
            return <li key={member.key}><div><strong>{model?.display_name ?? member.model_id}</strong><small>{model ? `${model.provider} · ${modelFacts(model, language)}` : t("pools.modelInactive")}</small></div><div className="member-actions"><button type="button" onClick={() => removeMember(member.key)} aria-label={t("pools.removeModel", { model: model?.display_name ?? t("models.model") })}>{t("pools.remove")}</button></div></li>;
          })}</ul>
        </div>}
        </div>
      </section>}
      <div className="form-actions"><Link href="/model-pools" className="gateway-button gateway-button--ghost">{t("common.cancel")}</Link><button className="gateway-button" type="submit" disabled={state === "saving" || !name.trim() || !purpose.trim() || !ladderComplete || waitingForSuggestions}>{state === "saving" ? t("common.saving") : editing ? t("providers.saveChanges") : t("pools.createTitle")}</button></div>
    </form>
  </>;
}
