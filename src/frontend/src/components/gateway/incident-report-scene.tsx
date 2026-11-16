"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useLanguage } from "@/components/language-provider";
import { listApplications, reportIncident } from "@/lib/gateway-api";
import type { ApplicationSummary, Incident, IncidentReportType } from "@/lib/gateway-types";

const categories: IncidentReportType[] = ["application", "model", "logic", "fallback", "other"];

export function NewIncidentReportScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const [applications, setApplications] = useState<ApplicationSummary[]>([]);
  const [applicationId, setApplicationId] = useState("");
  const [type, setType] = useState<IncidentReportType>("application");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [requestId, setRequestId] = useState("");
  const [steps, setSteps] = useState("");
  const [expected, setExpected] = useState("");
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [ticket, setTicket] = useState<Incident | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applicationError, setApplicationError] = useState(false);
  const [applicationRetry, setApplicationRetry] = useState(0);
  const canReadApplications = permissions.includes("applications.read_own") || permissions.includes("applications.read_global");

  useEffect(() => {
    if (!canReadApplications) return;
    let active = true;
    async function loadApplications() {
      const items: ApplicationSummary[] = [];
      let page = 1;
      while (active) {
        const response = await listApplications({ scope: "own", page, page_size: 100 });
        items.push(...response.items);
        if (items.length >= response.total || response.items.length === 0) break;
        page += 1;
      }
      if (active) { setApplications(items); setApplicationError(false); }
    }
    void loadApplications().catch(() => { if (active) setApplicationError(true); });
    return () => { active = false; };
  }, [canReadApplications, applicationRetry]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current || !title.trim() || !description.trim()) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const created = await reportIncident({
        title: title.trim(), description: description.trim(), type,
        application_id: applicationId || undefined,
        provider: provider.trim() || undefined, model: model.trim() || undefined,
        request_id: requestId.trim() || undefined,
        steps_to_reproduce: steps.trim(), expected_behavior: expected.trim(),
      });
      setTicket(created);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("reports.submitError"));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  if (ticket) return <section className="gateway-panel report-success">
    <h1>{t("reports.title")}</h1>
    <p role="status">{t("reports.success", { id: ticket.id })}</p>
    <p>{ticket.title}</p>
    <Link href="/reports" className="gateway-button">{t("reports.track")}</Link>
  </section>;

  return <>
    <section className="gateway-page-heading"><div><p className="gateway-eyebrow">{t("reports.title")}</p><h1>{t("reports.new")}</h1><p>{t("reports.formDescription")}</p></div></section>
    {error && <div className="notice notice--warning" role="alert">{error}</div>}
    {applicationError && <div className="notice notice--warning" role="status">{t("reports.applicationsError")} <button type="button" className="gateway-button gateway-button--ghost" onClick={() => setApplicationRetry((value) => value + 1)}>{t("common.tryAgain")}</button></div>}
    <form className="gateway-panel form-panel" onSubmit={submit}>
      <div className="form-section"><div><h2>{t("incidents.details")}</h2><p>{t("reports.descriptionHint")}</p></div><div className="form-grid">
        <label className="field full"><span>{t("incidents.titleField")} *</span><input required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
        <label className="field"><span>{t("reports.category")}</span><select value={type} onChange={(event) => setType(event.target.value as IncidentReportType)}>{categories.map((category) => <option key={category} value={category}>{t(`reports.${category}`)}</option>)}</select></label>
        <label className="field"><span>{t("reports.application")}</span><select value={applicationId} onChange={(event) => setApplicationId(event.target.value)}><option value="">{t("reports.noApplication")}</option>{applications.map((application) => <option key={application.id} value={application.id}>{application.name}</option>)}</select></label>
        <label className="field full"><span>{t("incidents.descriptionField")} *</span><textarea required maxLength={10000} rows={6} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
        <label className="field"><span>{t("reports.provider")}</span><input maxLength={100} value={provider} onChange={(event) => setProvider(event.target.value)} /></label>
        <label className="field"><span>{t("reports.modelField")}</span><input maxLength={150} value={model} onChange={(event) => setModel(event.target.value)} /></label>
        <label className="field full"><span>{t("reports.request")}</span><input maxLength={128} value={requestId} onChange={(event) => setRequestId(event.target.value)} /></label>
        <label className="field full"><span>{t("reports.steps")}</span><textarea maxLength={5000} rows={4} value={steps} onChange={(event) => setSteps(event.target.value)} /></label>
        <label className="field full"><span>{t("reports.expected")}</span><textarea maxLength={5000} rows={3} value={expected} onChange={(event) => setExpected(event.target.value)} /></label>
      </div></div>
      <div className="form-actions"><Link href="/reports" className="gateway-button gateway-button--ghost">{t("common.cancel")}</Link><button type="submit" className="gateway-button" disabled={busy || !title.trim() || !description.trim()}>{t(busy ? "reports.submitting" : "reports.submit")}</button></div>
    </form>
  </>;
}
