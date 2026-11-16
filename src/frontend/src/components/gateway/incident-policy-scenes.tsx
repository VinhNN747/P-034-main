"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useLanguage } from "@/components/language-provider";

import {
  createIncident,
  decidePolicyReview,
  listIncidents,
  listIncidentReports,
  updateIncident,
  listPolicyReviews,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type {
  Incident,
  IncidentSeverity,
  IncidentStatus,
  PolicyDecision,
  PolicyReview,
} from "@/lib/gateway-types";
import { PaginationControls, TABLE_PAGE_SIZE } from "@/components/gateway/pagination-controls";
import { ListPageSkeleton, PolicyReviewSkeleton } from "@/components/gateway/gateway-page-skeletons";

function message(error: unknown, fallback: string): string {
  return error && typeof error === "object" && "message" in error
    ? String((error as GatewayApiError).message)
    : fallback;
}

function State({ text, error = false }: { text: string; error?: boolean }) {
  return <div className={error ? "notice notice--warning" : "notice"} role="status">{text}</div>;
}

const severityTone: Record<IncidentSeverity, string> = {
  low: "healthy",
  medium: "watch",
  high: "incident",
  critical: "incident",
};

export function IncidentTicket({ incident, canManage, onUpdated }: { incident: Incident; canManage: boolean; onUpdated: () => void }) {
  const { t, language } = useLanguage();
  const [status, setStatus] = useState(incident.status);
  const [severity, setSeverity] = useState(incident.severity);
  const [owner, setOwner] = useState(incident.owner);
  const [note, setNote] = useState(incident.resolution_note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const categories = { application: t("reports.application"), model: t("reports.model"), logic: t("reports.logic"), fallback: t("reports.fallback"), other: t("reports.other") };
  const category = Object.hasOwn(categories, incident.type) ? categories[incident.type as keyof typeof categories] : incident.type;

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setSaved(false);
    setError(null);
    try {
      await updateIncident(incident.id, { status, severity, owner: owner.trim() || "Unassigned", resolution_note: note.trim() });
      setSaved(true);
      onUpdated();
    } catch (reason) {
      setError(message(reason, t("reports.updateError")));
    } finally { setBusy(false); }
  }

  return <article className="incident-ticket">
    <div className="incident-ticket-main">
      <div className="incident-ticket-kicker"><span className="incident-ticket-category">{category}</span><span>{new Date(incident.opened_at).toLocaleDateString(language === "vi" ? "vi-VN" : "en-US")}</span></div>
    <div><h2>{incident.title}</h2><p>{incident.scope} · {t("incidents.owner")}: {incident.owner}{incident.model ? ` · ${incident.model}` : ""}</p>{incident.source === "user_report" && <p>{t("reports.userReport")} · {t("reports.reporter")}: {incident.reporter_email ?? incident.reporter_user_id}</p>}</div>
    </div>
    <div className="incident-ticket-status"><span className={`gateway-status gateway-status--${severityTone[incident.severity]}`}>{t(`incidents.${incident.severity}`)}</span><span className={`gateway-status gateway-status--${incident.status === "resolved" ? "healthy" : "neutral"}`}>{t(`incidents.${incident.status}`)}</span></div>
    <details className="incident-ticket-details"><summary>{t("reports.details")}</summary><div className="incident-ticket-content">
      <p><strong>{t("reports.opened")}:</strong> {new Date(incident.opened_at).toLocaleString(language === "vi" ? "vi-VN" : "en-US")}</p>
      <p className="incident-ticket-description">{incident.description}</p>
      <dl className="incident-ticket-context">
        <div><dt>{t("reports.category")}</dt><dd>{category}</dd></div>
        {incident.application_id && <div><dt>{t("reports.application")}</dt><dd>{incident.application_id}</dd></div>}
        {incident.provider && <div><dt>{t("reports.provider")}</dt><dd>{incident.provider}</dd></div>}
        {incident.model && <div><dt>{t("reports.model")}</dt><dd>{incident.model}</dd></div>}
        {incident.request_id && <div><dt>{t("reports.requestId")}</dt><dd>{incident.request_id}</dd></div>}
        {incident.steps_to_reproduce && <div><dt>{t("reports.steps")}</dt><dd>{incident.steps_to_reproduce}</dd></div>}
        {incident.expected_behavior && <div><dt>{t("reports.expected")}</dt><dd>{incident.expected_behavior}</dd></div>}
        <div><dt>{t("incidents.customerImpact")}</dt><dd>{incident.impact}</dd></div>
      </dl>
      {!canManage && incident.resolution_note && <div><h3>{t("reports.note")}</h3><p className="incident-ticket-description">{incident.resolution_note}</p></div>}
      {canManage && <form onSubmit={save} className="form-grid">
        <label className="field"><span>{t("incidents.status")}</span><select value={status} onChange={(event) => setStatus(event.target.value as IncidentStatus)}>{(["investigating", "monitoring", "resolved"] as const).map((value) => <option key={value} value={value}>{t(`incidents.${value}`)}</option>)}</select></label>
        <label className="field"><span>{t("incidents.severity")}</span><select value={severity} onChange={(event) => setSeverity(event.target.value as IncidentSeverity)}>{(["low", "medium", "high", "critical"] as const).map((value) => <option key={value} value={value}>{t(`incidents.${value}`)}</option>)}</select></label>
        <label className="field full"><span>{t("incidents.owner")}</span><input value={owner} onChange={(event) => setOwner(event.target.value)} /></label>
        <label className="field full"><span>{t("reports.note")}</span><textarea rows={4} maxLength={10000} value={note} onChange={(event) => setNote(event.target.value)} /></label>
        {error && <div role="alert" className="notice notice--warning full">{error}</div>}
        {saved && <div role="status" className="notice full">{t("reports.saved")}</div>}
        <div className="full"><button type="submit" className="gateway-button" disabled={busy}>{t(busy ? "common.saving" : "common.save")}</button></div>
      </form>}
    </div></details>
  </article>;
}

export function IncidentsScene({ permissions, reportsOnly = false }: { permissions: readonly string[]; reportsOnly?: boolean }) {
  const { t } = useLanguage();
  const [items, setItems] = useState<Incident[]>([]);
  const [severity, setSeverity] = useState("");
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const pageSize = TABLE_PAGE_SIZE;
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const canManage = !reportsOnly && permissions.includes("incidents.manage");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let active = true;
    const load = reportsOnly ? listIncidentReports : listIncidents;
    void load({
      severity: (severity || undefined) as IncidentSeverity | undefined,
      status: (status || undefined) as IncidentStatus | undefined,
      search: search || undefined,
      page,
      page_size: pageSize,
    }).then((response) => {
      if (!active) return;
      setItems(response.items);
      setTotal(response.total);
      setState("ready");
      setError(null);
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("error");
      setError(message(reason, t("incidents.loadError")));
    });
    return () => { active = false; };
  }, [page, pageSize, search, severity, status, refresh, reportsOnly, t]);

  function setFilter(setter: (value: string) => void, value: string) {
    setter(value);
    setPage(1);
  }

  if (state === "loading") return <ListPageSkeleton label={t("incidents.loading")} filters={3} columns={3} />;

  return <>
    <section className="gateway-page-heading"><div><p className="gateway-eyebrow">{t(reportsOnly ? "reports.title" : "incidents.operational")}</p><h1>{t(reportsOnly ? "reports.title" : "incidents.title")}</h1><p>{t(reportsOnly ? "reports.description" : "incidents.description")}</p></div>{reportsOnly ? <Link href="/reports/new" className="gateway-button">{t("reports.new")}</Link> : canManage && <Link href="/incidents/new" className="gateway-button">{t("incidents.create")}</Link>}</section>
    <section className="filter-bar" aria-label={t("filter.filters")}><label><span>{t("incidents.search")}</span><input type="search" value={search} onChange={(event) => setFilter(setSearch, event.target.value)} placeholder={t("incidents.searchPlaceholder")} /></label><label><span>{t("incidents.severity")}</span><select value={severity} onChange={(event) => setFilter(setSeverity, event.target.value)}><option value="">{t("incidents.allSeverities")}</option><option value="low">{t("incidents.low")}</option><option value="medium">{t("incidents.medium")}</option><option value="high">{t("incidents.high")}</option><option value="critical">{t("incidents.critical")}</option></select></label><label><span>{t("incidents.status")}</span><select value={status} onChange={(event) => setFilter(setStatus, event.target.value)}><option value="">{t("incidents.allStatuses")}</option><option value="investigating">{t("incidents.investigating")}</option><option value="monitoring">{t("incidents.monitoring")}</option><option value="resolved">{t("incidents.resolved")}</option></select></label></section>
    {reportsOnly && <section className="reports-history-header" aria-labelledby="reports-history-title"><div><p className="gateway-eyebrow">{t("reports.historyEyebrow")}</p><h2 id="reports-history-title">{t("reports.historyTitle")}</h2><p>{t("reports.historyDescription")}</p></div><div className="reports-history-actions"><span>{t("reports.historyCount", { count: total })}</span><button type="button" className="gateway-button gateway-button--ghost" onClick={() => setRefresh((value) => value + 1)}>{t(state === "error" ? "common.tryAgain" : "reports.refresh")}</button></div></section>}
    {!reportsOnly && <button type="button" className="gateway-button gateway-button--ghost incidents-refresh-button" onClick={() => setRefresh((value) => value + 1)}>{t(state === "error" ? "common.tryAgain" : "reports.refresh")}</button>}
    {state === "error" && <State text={error ?? t("reports.loadError")} error />}
    {state === "ready" && items.length === 0 && <State text={t(reportsOnly ? "reports.empty" : "incidents.noMatch")} />}
    {state === "ready" && items.length > 0 && <section className={`gateway-panel incident-list ${reportsOnly ? "reports-history-list" : ""}`} aria-label={reportsOnly ? t("reports.historyTitle") : t("incidents.title")}><div className="reports-history-list-heading">{reportsOnly && <><strong>{t("reports.historyListHeading")}</strong><span>{t("reports.historyListHint")}</span></>}</div>{items.map((incident) => <IncidentTicket key={incident.id} incident={incident} canManage={canManage} onUpdated={() => setRefresh((value) => value + 1)} />)}<PaginationControls page={page} pageSize={pageSize} total={total} onPageChange={setPage} label={t("incidents.pagination")} /></section>}
  </>;
}

export function NewIncidentScene() {
  const { t } = useLanguage();
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState<IncidentSeverity>("medium");
  const [status, setStatus] = useState<IncidentStatus>("investigating");
  const [owner, setOwner] = useState("");
  const [type, setType] = useState("provider_outage");
  const [scope, setScope] = useState("");
  const [impact, setImpact] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setState("saving");
    try {
      await createIncident({ title, description, severity, status, owner: owner || "Unassigned", type, scope: scope || "Gateway", impact: impact || "Unknown" });
      router.push("/incidents");
    } catch (reason: unknown) {
      setState("error");
      setError(message(reason, t("incidents.createError")));
    }
  }

  return <><section className="gateway-page-heading"><div><p className="gateway-eyebrow">{t("incidents.operational")}</p><h1>{t("incidents.create")}</h1><p>{t("incidents.createDescription")}</p></div></section>{state === "error" && <State text={error ?? t("incidents.error")} error />}<form className="gateway-panel form-panel" onSubmit={submit}><div className="form-section"><div><p className="gateway-eyebrow">{t("incidents.details")}</p><h2>{t("incidents.scopeImpact")}</h2><p>{t("incidents.detailsDescription")}</p></div><div className="form-grid"><label className="field full"><span>{t("incidents.titleField")}</span><input required value={title} onChange={(event) => setTitle(event.target.value)} placeholder={t("incidents.titlePlaceholder")} /></label><label className="field"><span>{t("incidents.severity")} *</span><select value={severity} onChange={(event) => setSeverity(event.target.value as IncidentSeverity)}><option value="low">{t("incidents.low")}</option><option value="medium">{t("incidents.medium")}</option><option value="high">{t("incidents.high")}</option><option value="critical">{t("incidents.critical")}</option></select></label><label className="field"><span>{t("incidents.initialStatus")}</span><select value={status} onChange={(event) => setStatus(event.target.value as IncidentStatus)}><option value="investigating">{t("incidents.investigating")}</option><option value="monitoring">{t("incidents.monitoring")}</option></select></label><label className="field"><span>{t("incidents.type")}</span><select value={type} onChange={(event) => setType(event.target.value)}><option value="provider_outage">{t("incidents.providerOutage")}</option><option value="elevated_latency">{t("incidents.elevatedLatency")}</option><option value="error_spike">{t("incidents.errorSpike")}</option><option value="rate_limit">{t("incidents.rateLimit")}</option><option value="budget">{t("incidents.budget")}</option><option value="routing">{t("incidents.routing")}</option></select></label><label className="field full"><span>{t("incidents.affectedScope")}</span><input required value={scope} onChange={(event) => setScope(event.target.value)} placeholder={t("incidents.scopePlaceholder")} /></label><label className="field full"><span>{t("incidents.descriptionField")}</span><textarea required rows={4} value={description} onChange={(event) => setDescription(event.target.value)} /></label><label className="field full"><span>{t("incidents.customerImpact")}</span><textarea rows={3} value={impact} onChange={(event) => setImpact(event.target.value)} /></label><label className="field"><span>{t("incidents.owner")}</span><input value={owner} onChange={(event) => setOwner(event.target.value)} placeholder={t("incidents.ownerPlaceholder")} /></label></div></div><div className="form-actions"><Link href="/incidents" className="gateway-button gateway-button--ghost">{t("common.cancel")}</Link><button type="submit" className="gateway-button" disabled={state === "saving"}>{state === "saving" ? t("common.saving") : t("incidents.create")}</button></div></form></>;
}

export function PolicyReviewsScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const [items, setItems] = useState<PolicyReview[]>([]);
  const [selected, setSelected] = useState<PolicyReview | null>(null);
  const [comment, setComment] = useState("");
  const [pending, setPending] = useState<PolicyDecision | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const pageSize = TABLE_PAGE_SIZE;
  const canDecide = permissions.includes("routing_policies.manage");
  const policyStatusLabel = (value: PolicyReview["status"]) => {
    if (value === "pending") return t("policies.statusPending");
    if (value === "approved") return t("policies.statusApproved");
    if (value === "rejected") return t("policies.statusRejected");
    return t("policies.statusChangesRequested");
  };
  const policyDecisionLabel = (value: PolicyDecision) => value === "approve" ? t("policies.approve") : value === "reject" ? t("policies.reject") : t("policies.requestChanges");

  useEffect(() => {
    let active = true;
    void listPolicyReviews({ page, page_size: pageSize }).then((response) => {
      if (!active) return;
      setItems(response.items);
      setSelected(response.items[0] ?? null);
      setTotal(response.total);
      setState("ready");
      setError(null);
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("error");
      setError(message(reason, t("policies.loadError")));
    });
    return () => { active = false; };
  }, [page, pageSize, t]);

  async function decide(decision: PolicyDecision) {
    if (!selected) return;
    try {
      const updated = await decidePolicyReview(selected.id, { decision, comment });
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
      setSelected(updated);
      setPending(null);
      setComment("");
    } catch (reason: unknown) {
      setError(message(reason, t("policies.decisionError")));
    }
  }

  if (state === "loading") return <PolicyReviewSkeleton />;
  if (state === "error") return <State text={error ?? t("policies.loadError")} error />;
  return <>
    <section className="gateway-page-heading"><div><p className="gateway-eyebrow">{t("policies.humanLoop")}</p><h1>{t("policies.title")}</h1><p>{t("policies.description")}</p></div><Link href="/dashboard" className="gateway-button gateway-button--ghost">{t("policies.backOverview")}</Link></section>
    {items.length === 0 && <State text={t("policies.empty")} />}
    <div className="review-layout">
      {items.length > 0 && <section className="gateway-panel review-queue"><div className="queue-tabs"><span className="is-active">{t("policies.all")} <b>{total}</b></span></div>{items.map((item) => <button type="button" className={selected?.id === item.id ? "queue-item is-active" : "queue-item"} onClick={() => { setSelected(item); setPending(null); setComment(""); }} key={item.id}><span>{item.id} · {item.scope}</span><strong>{item.name}</strong><small>{item.submitted_by} · {item.type}</small><b>{policyStatusLabel(item.status)}</b></button>)}<PaginationControls page={page} pageSize={pageSize} total={total} onPageChange={setPage} label={t("policies.pagination")} /></section>}
      {selected && <section className="gateway-panel review-detail"><div className="gateway-section-heading"><div><p className="gateway-eyebrow">{selected.id} · {selected.type}</p><h2>{selected.name}</h2></div><span className="gateway-status gateway-status--watch">{policyStatusLabel(selected.status)}</span></div><p className="detail-copy">{selected.scope}</p><dl className="review-list"><div><dt>{t("policies.submittedBy")}</dt><dd>{selected.submitted_by}</dd></div><div><dt>{t("policies.affectedModels")}</dt><dd>{selected.affected_models.join(", ") || t("policies.none")}</dd></div><div><dt>{t("policies.affectedApplications")}</dt><dd>{selected.affected_applications.join(", ") || t("policies.none")}</dd></div></dl><h3>{t("policies.decisionHistory")}</h3>{selected.decision_history.length === 0 ? <p className="detail-copy">{t("policies.noDecisions")}</p> : <ul className="gateway-signal-list">{selected.decision_history.map((item) => <li key={item.id}>{policyDecisionLabel(item.decision)} · {item.actor_user_id} · {item.comment}</li>)}</ul>}{canDecide && selected.status === "pending" && <><label className="field"><span>{t("policies.comment")}</span><textarea rows={3} value={comment} onChange={(event) => setComment(event.target.value)} placeholder={t("policies.commentPlaceholder")} /></label>{pending && <div className="decision-confirm" role="alert"><strong>{t("policies.confirmDecision", { decision: policyDecisionLabel(pending) })}</strong><button type="button" className="gateway-button gateway-button--ghost" onClick={() => setPending(null)}>{t("common.cancel")}</button><button type="button" className="gateway-button" onClick={() => void decide(pending)}>{t("policies.confirm")}</button></div>}<div className="decision-actions"><button type="button" className="gateway-button gateway-button--ghost" onClick={() => setPending("request_changes")}>{t("policies.requestChanges")}</button><button type="button" className="gateway-button gateway-button--danger" onClick={() => setPending("reject")}>{t("policies.reject")}</button><button type="button" className="gateway-button" onClick={() => setPending("approve")}>{t("policies.approve")}</button></div></>}</section>}
    </div>
  </>;
}
