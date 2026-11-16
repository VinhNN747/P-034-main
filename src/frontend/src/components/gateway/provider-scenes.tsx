"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLanguage } from "@/components/language-provider";

import {
  createProviderConnection,
  listProviderConnections,
  listServiceProviderBindings,
  probeProviderConnection,
  replaceProviderCredential,
  revokeProviderCredential,
  setServiceProviderBinding,
  updateProviderConnection,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type {
  CredentialSummary,
  ProbeMode,
  ProviderConnection,
  ProviderConnectionProbeResult,
  ProviderKind,
  ProviderPurpose,
  ServiceBindingPurpose,
  ServiceProviderBinding,
} from "@/lib/gateway-types";
import { PaginationControls, TABLE_PAGE_SIZE, useClientTablePagination } from "@/components/gateway/pagination-controls";
import { FormPageSkeleton } from "@/components/gateway/gateway-page-skeletons";

const bindingPurposes: Array<{ purpose: ServiceBindingPurpose; label: string; kind: ProviderKind }> = [
  { purpose: "internal_openai", label: "OpenAI internal services", kind: "openai" },
  { purpose: "ocr_google", label: "Gemini OCR", kind: "google" },
  { purpose: "routing_jev", label: "JEV routing", kind: "typesafe" },
];

export const providerKindOptions: Array<{ kind: ProviderKind; label: string }> = [
  { kind: "openai", label: "OpenAI" },
  { kind: "google", label: "Google / Gemini" },
  { kind: "openrouter", label: "OpenRouter" },
  { kind: "anthropic", label: "Anthropic" },
  { kind: "typesafe", label: "TypeSafe / JEV" },
  { kind: "offline", label: "Offline" },
  { kind: "local", label: "Local" },
];

export const ANTHROPIC_BASE_URL = "https://api.anthropic.com";

export function providerPurposeOptions(kind: ProviderKind): ProviderPurpose[] {
  if (kind === "anthropic") return ["target"];
  if (kind === "typesafe") return ["internal"];
  return ["target", "internal"];
}

const probeModesByKind: Record<ProviderKind, ProbeMode[]> = {
  openai: ["chat", "embedding"],
  google: ["chat", "ocr"],
  openrouter: ["chat"],
  anthropic: ["chat"],
  typesafe: ["jev"],
  offline: ["chat"],
  local: ["chat"],
};

export function providerProbeModes(kind: ProviderKind): ProbeMode[] {
  return probeModesByKind[kind];
}

export function providerProbeVersionMatches(resultVersion: number | null, expectedVersion: number): boolean {
  return resultVersion === expectedVersion || (resultVersion === null && expectedVersion === 0);
}

export function canProbeProviderConnection(
  credential: Pick<CredentialSummary, "configured" | "status">,
  kind?: ProviderKind,
): boolean {
  if (kind === "offline" || kind === "local") return true;
  return credential.configured && credential.status === "active";
}

export function providerCredentialLabel(credential: CredentialSummary, kind: ProviderKind): string {
  if (credential.status === "revoked") return `revoked${credential.key_last4 ? ` · ••••${credential.key_last4}` : ""}${credential.version ? ` · v${credential.version}` : ""}`;
  if (credential.configured) return `configured${credential.key_last4 ? ` · ••••${credential.key_last4}` : ""}${credential.version ? ` · v${credential.version}` : ""}`;
  return kind === "offline" || kind === "local" ? "Not required" : "Not configured";
}

export function providerTestStatus(credential: CredentialSummary): NonNullable<CredentialSummary["last_test_status"]> {
  if (credential.last_test_credential_version !== credential.version) return "untested";
  return credential.last_test_status ?? "untested";
}

export function providerProbeErrorMessage(code?: string | null): string {
  switch (code) {
    case "AUTHENTICATION_FAILED": return "The provider rejected this key. Check the credential and test again.";
    case "ACCESS_DENIED": return "The provider denied access to this model or action.";
    case "MODEL_NOT_FOUND": return "The upstream model ID was not found. Check the ID and test again.";
    case "QUOTA_EXCEEDED": return "The provider account has no available quota.";
    case "RATE_LIMITED": return "The provider rate limit was reached. Wait and test again.";
    case "TIMEOUT": return "The connection test timed out.";
    case "NETWORK_ERROR": return "The provider could not be reached.";
    case "SKIPPED_OFFLINE": return "Skipped in offline execution mode.";
    case "CREDENTIAL_UNAVAILABLE": return "This connection has no active credential.";
    default: return "The connection test failed. Check provider settings and try again.";
  }
}

function safeApiError(error: unknown): string {
  if (error && typeof error === "object" && "code" in error) {
    const apiError = error as GatewayApiError;
    if (apiError.code === "conflict") return "This record changed elsewhere. Reload the provider summary and retry.";
    if (apiError.status === 401 || apiError.status === 403) return "You do not have permission to manage provider connections.";
    if (apiError.status === 503) return "The provider management service is unavailable. Try again shortly.";
  }
  return "The request could not be completed. Check the form and try again.";
}

function probeSummary(credential: CredentialSummary) {
  return { status: providerTestStatus(credential) };
}

function ProviderDialogFrame({
  eyebrow,
  title,
  description,
  pending,
  onClose,
  children,
  className,
}: {
  eyebrow: string;
  title: string;
  description?: string;
  pending: boolean;
  onClose: () => void;
  children: React.ReactNode;
  className?: string;
}) {
  const { t } = useLanguage();
  const titleId = useId();
  return <div className="gateway-modal-backdrop provider-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !pending) onClose(); }}>
    <section className={`gateway-panel provider-dialog${className ? ` ${className}` : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={(event) => { if (event.key === "Escape" && !pending) onClose(); }}>
      <header className="provider-dialog-header">
        <div><p className="gateway-eyebrow">{eyebrow}</p><h2 id={titleId}>{title}</h2>{description && <p>{description}</p>}</div>
        <button type="button" className="gateway-icon-button provider-dialog-close" aria-label={t("providers.closeDialog")} disabled={pending} onClick={onClose}>×</button>
      </header>
      {children}
    </section>
  </div>;
}

export function ProviderProbeDialog({
  connection,
  pending,
  onClose,
  onRun,
}: {
  connection: ProviderConnection;
  pending: boolean;
  onClose: () => void;
  onRun: (input: { mode: ProbeMode; model_name?: string }) => Promise<void>;
}) {
  const { t } = useLanguage();
  const modes = providerProbeModes(connection.kind);
  const [mode, setMode] = useState<ProbeMode>(modes[0]);
  const [modelName, setModelName] = useState("");
  return <ProviderDialogFrame eyebrow={t("providers.connectionTest")} title={`${t("providers.ping")} · ${connection.display_name}`} description={t("providers.testDescription")} pending={pending} onClose={onClose}>
    <form className="provider-dialog-form" onSubmit={(event) => { event.preventDefault(); void onRun({ mode, ...(modelName.trim() ? { model_name: modelName.trim() } : {}) }); }}>
      <div className="form-grid provider-probe-fields">
        <label className="field"><span>{t("providers.test")}</span><select value={mode} onChange={(event) => setMode(event.target.value as ProbeMode)}>{modes.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
        <label className="field"><span>{t("providers.modelId")}</span><input value={modelName} onChange={(event) => setModelName(event.target.value)} placeholder={t("providers.leaveBlank")} /></label>
      </div>
      <div className="form-actions provider-dialog-actions"><button className="gateway-button gateway-button--ghost" type="button" onClick={onClose} disabled={pending}>{t("common.cancel")}</button><button className="gateway-button" type="submit" disabled={pending}>{pending ? t("providers.testing") : t("providers.runTest")}</button></div>
    </form>
  </ProviderDialogFrame>;
}

function ProviderConnectionEditDialog({
  connection,
  pending,
  onClose,
  onSave,
}: {
  connection: ProviderConnection;
  pending: boolean;
  onClose: () => void;
  onSave: (event: React.FormEvent<HTMLFormElement>) => void;
}) {
  const { t } = useLanguage();
  return <ProviderDialogFrame eyebrow={t("providers.connectionSettings")} title={`${t("providers.edit")} ${connection.display_name}`} description={t("providers.editDescription")} pending={pending} onClose={onClose}>
    <form className="provider-dialog-form" onSubmit={onSave}>
      <div className="form-grid provider-edit-fields">
        <label className="field full"><span>{t("providers.displayName")}</span><input name="display_name" required defaultValue={connection.display_name} /></label>
        <label className="field full"><span>{t("providers.connectionStatus")}</span><select name="status" defaultValue={connection.status}><option value="active">{t("models.active")}</option><option value="inactive">{t("models.inactive")}</option></select></label>
      </div>
      <div className="form-actions provider-dialog-actions"><button className="gateway-button gateway-button--ghost" type="button" onClick={onClose} disabled={pending}>{t("common.cancel")}</button><button className="gateway-button" type="submit" disabled={pending}>{pending ? t("providers.saving") : t("providers.saveChanges")}</button></div>
    </form>
  </ProviderDialogFrame>;
}

function ProviderCredentialDialog({
  connection,
  pending,
  onClose,
  onSave,
}: {
  connection: ProviderConnection;
  pending: boolean;
  onClose: () => void;
  onSave: (event: React.FormEvent<HTMLFormElement>) => void;
}) {
  const { t } = useLanguage();
  return <ProviderDialogFrame eyebrow={t("providers.credential")} title={`${t("providers.replaceKey")} · ${connection.display_name}`} description={`Current key: ${providerCredentialLabel(connection.credential, connection.kind)}. The new key is stored securely.`} pending={pending} onClose={onClose}>
    <form className="provider-dialog-form" onSubmit={onSave}>
      <div className="form-grid provider-edit-fields">
        <label className="field full"><span>{t("providers.newApiKey")}</span><input name="api_key" type="password" autoComplete="new-password" required minLength={5} autoFocus /></label>
      </div>
      <div className="form-actions provider-dialog-actions"><button className="gateway-button gateway-button--ghost" type="button" onClick={onClose} disabled={pending}>{t("common.cancel")}</button><button className="gateway-button" type="submit" disabled={pending}>{pending ? t("providers.saving") : t("providers.saveKey")}</button></div>
    </form>
  </ProviderDialogFrame>;
}

export function ProviderCreateDialog({
  name,
  displayName,
  kind,
  purpose,
  baseUrl,
  newKey,
  error,
  pending,
  onNameChange,
  onDisplayNameChange,
  onKindChange,
  onPurposeChange,
  onBaseUrlChange,
  onNewKeyChange,
  onClose,
  onSave,
}: {
  name: string;
  displayName: string;
  kind: ProviderKind;
  purpose: ProviderPurpose;
  baseUrl: string;
  newKey: string;
  error: string | null;
  pending: boolean;
  onNameChange: (value: string) => void;
  onDisplayNameChange: (value: string) => void;
  onKindChange: (value: ProviderKind) => void;
  onPurposeChange: (value: ProviderPurpose) => void;
  onBaseUrlChange: (value: string) => void;
  onNewKeyChange: (value: string) => void;
  onClose: () => void;
  onSave: (event: React.FormEvent<HTMLFormElement>) => void;
}) {
  const { t } = useLanguage();
  function changeProviderKind(nextKind: ProviderKind) {
    onKindChange(nextKind);
    const allowedPurposes = providerPurposeOptions(nextKind);
    if (!allowedPurposes.includes(purpose)) onPurposeChange(allowedPurposes[0]);
    onBaseUrlChange(nextKind === "anthropic" ? ANTHROPIC_BASE_URL : "");
  }
  return <ProviderDialogFrame className="provider-create-dialog" eyebrow={t("providers.catalogue")} title={t("providers.addConnection")} description={t("providers.addDescription")} pending={pending} onClose={onClose}>
    <form className="provider-dialog-form" onSubmit={onSave}>
      {error && <div className="notice notice--warning provider-dialog-error" role="alert">{error}</div>}
      <div className="form-grid provider-create-fields">
        <label className="field"><span>{t("providers.internalName")}</span><input autoFocus required value={name} onChange={(event) => onNameChange(event.target.value)} /></label>
        <label className="field"><span>{t("providers.displayName")} *</span><input required value={displayName} onChange={(event) => onDisplayNameChange(event.target.value)} /></label>
        <label className="field"><span>{t("providers.providerKind")}</span><select value={kind} onChange={(event) => changeProviderKind(event.target.value as ProviderKind)}>{providerKindOptions.map((item) => <option key={item.kind} value={item.kind}>{item.label}</option>)}</select></label>
        <label className="field"><span>{t("providers.purpose")}</span><select value={purpose} onChange={(event) => onPurposeChange(event.target.value as ProviderPurpose)}>{providerPurposeOptions(kind).map((item) => <option key={item} value={item}>{item === "target" ? t("providers.targetModel") : t("providers.internalService")}</option>)}</select></label>
        <label className="field full"><span>{t("providers.baseUrl")}</span><input value={baseUrl} disabled={kind === "anthropic"} onChange={(event) => onBaseUrlChange(event.target.value)} placeholder={t("providers.customEndpoint")} />{kind === "anthropic" && <small>{t("providers.anthropicBaseUrlFixed")}</small>}</label>
        <label className="field full"><span>{t("providers.apiKeyOptional")}</span><input type="password" autoComplete="new-password" value={newKey} onChange={(event) => onNewKeyChange(event.target.value)} /></label>
      </div>
      <div className="form-actions provider-dialog-actions"><button type="button" className="gateway-button gateway-button--ghost" onClick={onClose} disabled={pending}>{t("common.cancel")}</button><button className="gateway-button" type="submit" disabled={pending}>{pending ? t("providers.saving") : t("providers.addConnection")}</button></div>
    </form>
  </ProviderDialogFrame>;
}

export function ProviderConnectionsScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [bindings, setBindings] = useState<ServiceProviderBinding[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [probeConnection, setProbeConnection] = useState<ProviderConnection | null>(null);
  const [editConnection, setEditConnection] = useState<ProviderConnection | null>(null);
  const [keyConnection, setKeyConnection] = useState<ProviderConnection | null>(null);
  const [probeResult, setProbeResult] = useState<ProviderConnectionProbeResult | null>(null);
  const [actionMenu, setActionMenu] = useState<{ connection: ProviderConnection; top: number; left: number } | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [kind, setKind] = useState<ProviderKind>("openai");
  const [purpose, setPurpose] = useState<ProviderPurpose>("target");
  const [baseUrl, setBaseUrl] = useState("");
  const [newKey, setNewKey] = useState("");
  const [bindingDrafts, setBindingDrafts] = useState<Record<string, string>>({});
  const probeTokens = useRef<Record<string, number>>({});
  const actionMenuRef = useRef<HTMLDivElement>(null);
  const actionTriggerRef = useRef<HTMLButtonElement>(null);
  const connectionPagination = useClientTablePagination(connections);

  const reload = useCallback(async () => {
    const [nextConnections, nextBindings] = await Promise.all([listProviderConnections(), listServiceProviderBindings()]);
    setConnections(nextConnections);
    setBindings(nextBindings);
    setBindingDrafts(Object.fromEntries(nextBindings.map((item) => [item.purpose, item.provider_id ?? ""])));
  }, []);

  useEffect(() => {
    let active = true;
    void Promise.all([listProviderConnections(), listServiceProviderBindings()]).then(([items, nextBindings]) => {
      if (!active) return;
      setConnections(items);
      setBindings(nextBindings);
      setBindingDrafts(Object.fromEntries(nextBindings.map((item) => [item.purpose, item.provider_id ?? ""])));
      setLoading(false);
    }).catch((reason: unknown) => { if (active) { setError(safeApiError(reason)); setLoading(false); } });
    return () => { active = false; };
  }, []);

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
    actionMenuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer, true);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", closeOnViewportChange);
      window.removeEventListener("scroll", closeOnViewportChange, true);
    };
  }, [actionMenu]);

  const internalConnections = useMemo(() => connections.filter((item) => item.purpose === "internal"), [connections]);

  async function saveNewConnection(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPendingId("create"); setError(null); setActionMessage(null);
    const payload = {
      name: name.trim(), display_name: displayName.trim(), kind, purpose,
      ...(baseUrl.trim() ? { base_url: baseUrl.trim() } : {}),
      ...(newKey ? { api_key: newKey } : {}),
    };
    try {
      await createProviderConnection(payload);
      setName(""); setDisplayName(""); setBaseUrl(""); setNewKey(""); setCreateOpen(false);
      await reload(); setActionMessage("Provider connection saved. Add or test its credential to verify it.");
    } catch (reason) { setError(safeApiError(reason)); }
    finally { setNewKey(""); setPendingId(null); }
  }

  function closeCreateDialog() {
    if (pendingId === "create") return;
    setCreateOpen(false);
    setNewKey("");
  }

  async function replaceKey(connection: ProviderConnection, input: HTMLInputElement) {
    const apiKey = input.value;
    if (!apiKey) return;
    setPendingId(connection.id); setError(null); setActionMessage(null);
    probeTokens.current[connection.id] = (probeTokens.current[connection.id] ?? 0) + 1;
    try {
      const updated = await replaceProviderCredential(connection.id, { api_key: apiKey, expected_version: connection.credential.version ?? 0 });
      setConnections((current) => current.map((item) => item.id === updated.id ? updated : item));
      setProbeResult(null); setActionMessage("Credential saved. Ping / Test it before relying on the connection.");
      await reload();
      setKeyConnection(null);
    } catch (reason) { setError(safeApiError(reason)); if (reason && typeof reason === "object" && "status" in reason && (reason as GatewayApiError).status === 409) await reload(); }
    finally { input.value = ""; setPendingId(null); }
  }

  async function revokeKey(connection: ProviderConnection) {
    if (!connection.credential.version || !window.confirm(`Revoke this key? Models or internal services using ${connection.display_name} will not be able to call the provider until a new key is saved.`)) return;
    setPendingId(connection.id); setError(null); setActionMessage(null);
    probeTokens.current[connection.id] = (probeTokens.current[connection.id] ?? 0) + 1;
    try { await revokeProviderCredential(connection.id, connection.credential.version); setProbeResult(null); await reload(); setActionMessage("Credential revoked. The connection remains in the catalog."); }
    catch (reason) { setError(safeApiError(reason)); if (reason && typeof reason === "object" && "status" in reason && (reason as GatewayApiError).status === 409) await reload(); }
    finally { setPendingId(null); }
  }

  async function saveConnectionMetadata(connection: ProviderConnection, event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setPendingId(connection.id); setError(null);
    try {
      const updated = await updateProviderConnection(connection.id, { display_name: String(form.get("display_name") ?? ""), status: String(form.get("status")) as "active" | "inactive" });
      setConnections((current) => current.map((item) => item.id === updated.id ? updated : item));
      setEditConnection(null);
      setActionMessage("Connection details updated.");
    }
    catch (reason) { setError(safeApiError(reason)); }
    finally { setPendingId(null); }
  }

  async function runProbe(input: { mode: ProbeMode; model_name?: string }) {
    const connection = probeConnection;
    if (!connection) return;
    setPendingId(connection.id); setError(null); setProbeResult(null);
    const token = (probeTokens.current[connection.id] ?? 0) + 1;
    probeTokens.current[connection.id] = token;
    try {
      const result = await probeProviderConnection(connection.id, { expected_version: connection.credential.version ?? 0, ...input });
      if (probeTokens.current[connection.id] !== token) return;
      if (!providerProbeVersionMatches(result.credential_version, connection.credential.version ?? 0)) {
        await reload(); setActionMessage("The credential changed while the test was running. Reloaded the current status; run the test again."); setProbeConnection(null); return;
      }
      setProbeResult(result); await reload(); setProbeConnection(null);
    } catch (reason) {
      if (probeTokens.current[connection.id] !== token) return;
      setError(safeApiError(reason));
      if (reason && typeof reason === "object" && "status" in reason && (reason as GatewayApiError).status === 409) { await reload(); setActionMessage("The key changed during this test. Reloaded the latest summary; run the test again."); }
      setProbeConnection(null);
    } finally { if (probeTokens.current[connection.id] === token) setPendingId(null); }
  }

  async function saveBinding(purposeValue: ServiceBindingPurpose) {
    setPendingId(purposeValue); setError(null);
    try { const next = await setServiceProviderBinding(purposeValue, { provider_id: bindingDrafts[purposeValue] || null }); setBindings(next); setBindingDrafts(Object.fromEntries(next.map((item) => [item.purpose, item.provider_id ?? ""]))); }
    catch (reason) { setError(safeApiError(reason)); }
    finally { setPendingId(null); }
  }

  function openActionMenu(connection: ProviderConnection, trigger: HTMLButtonElement) {
    if (actionMenu?.connection.id === connection.id) {
      setActionMenu(null);
      return;
    }
    const rect = trigger.getBoundingClientRect();
    const menuWidth = Math.min(232, window.innerWidth - 24);
    const menuHeight = connection.credential.configured && connection.credential.status === "active" ? 216 : 176;
    const left = Math.max(12, Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 12));
    const top = rect.bottom + menuHeight + 12 <= window.innerHeight
      ? rect.bottom + 8
      : Math.max(12, rect.top - menuHeight - 8);
    actionTriggerRef.current = trigger;
    setActionMenu({ connection, top, left });
  }

  function saveKeyFromDialog(event: React.FormEvent<HTMLFormElement>, connection: ProviderConnection) {
    event.preventDefault();
    const input = event.currentTarget.elements.namedItem("api_key") as HTMLInputElement | null;
    if (input) void replaceKey(connection, input);
  }

  if (!permissions.includes("models.manage")) return <div className="notice notice--warning" role="status">{t("workspace.accessDenied")}</div>;
  if (loading) return <FormPageSkeleton label={t("providers.connections")} fields={4} />;

  return <>
    <section className="gateway-page-heading"><div><p className="gateway-eyebrow">{t("providers.credentials")}</p><h1>{t("providers.title")}</h1><p>{t("providers.description")}</p></div><button type="button" className="gateway-button" onClick={() => { setCreateOpen(true); setNewKey(""); }}>{t("providers.addConnection")}</button></section>
    {error && !createOpen && <div className="notice notice--warning" role="alert">{error}</div>}
    {actionMessage && <div className="notice" role="status">{actionMessage}</div>}
    {createOpen && <ProviderCreateDialog
      name={name}
      displayName={displayName}
      kind={kind}
      purpose={purpose}
      baseUrl={baseUrl}
      newKey={newKey}
      error={error}
      pending={pendingId === "create"}
      onNameChange={setName}
      onDisplayNameChange={setDisplayName}
      onKindChange={(next) => { setKind(next); if (next === "typesafe") setPurpose("internal"); }}
      onPurposeChange={setPurpose}
      onBaseUrlChange={setBaseUrl}
      onNewKeyChange={setNewKey}
      onClose={closeCreateDialog}
      onSave={saveNewConnection}
    />}

    <section className="gateway-panel table-panel provider-connections-panel">
      <div className="gateway-page-heading provider-section-heading"><div><p className="gateway-eyebrow">{t("providers.modelTargets")}</p><h2>{t("providers.connections")}</h2></div><Link href="/models" className="gateway-button gateway-button--ghost">{t("providers.manageModels")}</Link></div>
      {connections.length === 0 ? <div className="notice">{t("providers.noConnections")}</div> : <><div className="gateway-table-wrap"><table className="gateway-table provider-connection-table">
        <thead><tr><th>{t("providers.connection")}</th><th>{t("providers.kindPurpose")}</th><th>{t("providers.status")}</th><th>{t("providers.credential")}</th><th>{t("providers.lastTest")}</th><th>{t("providers.actions")}</th></tr></thead>
        <tbody>{connectionPagination.pageItems.map((connection) => {
          const test = probeSummary(connection.credential);
          return <tr key={connection.id}>
            <td className="provider-connection-identity"><strong>{connection.display_name}</strong><small>{connection.name}{connection.base_url ? ` · ${connection.base_url}` : ""}</small></td>
            <td><span className="provider-kind-label">{connection.kind}</span><small>{connection.purpose}</small></td>
            <td><span className={"provider-connection-status provider-connection-status--" + connection.status}>{connection.status}</span></td>
            <td className="provider-credential-cell">{providerCredentialLabel(connection.credential, connection.kind)}</td>
            <td><div className="provider-test-summary">
              <span className={"gateway-status gateway-status--" + (test.status === "healthy" ? "healthy" : test.status === "failed" ? "watch" : "neutral")}><i className="status-pin" />{test.status}</span>
              <small>{connection.credential.last_test_at ? new Date(connection.credential.last_test_at).toLocaleString() : t("providers.noTests")}</small>
            </div></td>
            <td className="provider-actions-cell"><button
              ref={actionMenu?.connection.id === connection.id ? actionTriggerRef : undefined}
              type="button"
              className="gateway-icon-button provider-actions-trigger"
              aria-label={`${t("providers.actions")}: ${connection.display_name}`}
              aria-expanded={actionMenu?.connection.id === connection.id}
              aria-controls={actionMenu?.connection.id === connection.id ? "provider-actions-popup" : undefined}
              onClick={(event) => openActionMenu(connection, event.currentTarget)}
            >⋯</button></td>
          </tr>;
        })}</tbody>
      </table></div><PaginationControls page={connectionPagination.page} pageSize={TABLE_PAGE_SIZE} total={connections.length} onPageChange={connectionPagination.setPage} label={t("providers.pagination")} /></>}
    </section>

    <section className="gateway-panel provider-bindings-panel">
      <div className="provider-section-heading"><div><p className="gateway-eyebrow">{t("providers.internalServices")}</p><h2>{t("providers.bindings")}</h2><p>{t("providers.bindingDescription")}</p></div></div>
      <div className="provider-bindings-list">{bindingPurposes.map((definition) => {
        const binding = bindings.find((item) => item.purpose === definition.purpose);
        const boundConnection = connections.find((item) => item.id === binding?.provider_id);
        const credential = binding?.credential;
        const test = credential ? probeSummary(credential) : null;
        const choices = internalConnections.filter((item) => item.kind === definition.kind && item.status === "active");
        return <article className="provider-binding-row" key={definition.purpose}>
          <div className="provider-binding-service"><strong>{definition.label}</strong><small>{definition.kind}</small></div>
          <label className="field provider-binding-select"><span>{t("providers.connection")}</span><select value={bindingDrafts[definition.purpose] ?? ""} onChange={(event) => setBindingDrafts((current) => ({ ...current, [definition.purpose]: event.target.value }))}><option value="">{t("providers.notConfigured")}</option>{choices.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label>
          <div className="provider-binding-summary">
            <strong>{boundConnection?.display_name ?? t("providers.noConnection")}</strong>
            {boundConnection && <small>{providerCredentialLabel(credential ?? { configured: false }, boundConnection.kind)}</small>}
            <span className={"gateway-status gateway-status--" + (test?.status === "healthy" ? "healthy" : test?.status === "failed" ? "watch" : "neutral")}><i className="status-pin" />{test?.status ?? "untested"}</span>
          </div>
          <div className="provider-binding-actions">
            <button className="gateway-button gateway-button--ghost" type="button" disabled={pendingId === definition.purpose} onClick={() => void saveBinding(definition.purpose)}>{pendingId === definition.purpose ? t("providers.saving") : t("providers.saveBinding")}</button>
            {boundConnection && <button className="gateway-button gateway-button--ghost" type="button" disabled={!credential || !canProbeProviderConnection(credential, boundConnection.kind) || pendingId === boundConnection.id} onClick={() => { setProbeConnection(boundConnection); setProbeResult(null); }}>{t("providers.ping")}</button>}
          </div>
        </article>;
      })}</div>
    </section>
    {actionMenu && typeof document !== "undefined" && createPortal(<div id="provider-actions-popup" ref={actionMenuRef} className="provider-actions-popup" style={{ left: actionMenu.left, top: actionMenu.top }} aria-label={`${t("providers.actions")}: ${actionMenu.connection.display_name}`}>
      <p>{t("providers.actionsMenu")}</p>
      <button type="button" disabled={pendingId === actionMenu.connection.id || !canProbeProviderConnection(actionMenu.connection.credential, actionMenu.connection.kind)} onClick={() => { setActionMenu(null); setProbeConnection(actionMenu.connection); setProbeResult(null); }}>{t("providers.ping")}</button>
      <button type="button" disabled={pendingId === actionMenu.connection.id} onClick={() => { setActionMenu(null); setEditConnection(actionMenu.connection); }}>{t("providers.edit")}</button>
      <button type="button" disabled={pendingId === actionMenu.connection.id} onClick={() => { setActionMenu(null); setKeyConnection(actionMenu.connection); }}>{t("providers.replaceKey")}</button>
      {actionMenu.connection.credential.configured && actionMenu.connection.credential.status === "active" && <button type="button" className="provider-actions-danger" disabled={pendingId === actionMenu.connection.id} onClick={() => { setActionMenu(null); void revokeKey(actionMenu.connection); }}>{t("providers.revokeKey")}</button>}
    </div>, document.body)}
    {editConnection && <ProviderConnectionEditDialog connection={editConnection} pending={pendingId === editConnection.id} onClose={() => setEditConnection(null)} onSave={(event) => void saveConnectionMetadata(editConnection, event)} />}
    {keyConnection && <ProviderCredentialDialog connection={keyConnection} pending={pendingId === keyConnection.id} onClose={() => setKeyConnection(null)} onSave={(event) => saveKeyFromDialog(event, keyConnection)} />}
    {probeConnection && <ProviderProbeDialog connection={probeConnection} pending={pendingId === probeConnection.id} onClose={() => { setProbeConnection(null); setProbeResult(null); }} onRun={runProbe} />}
    {probeResult && <div className={`notice ${probeResult.status === "healthy" ? "" : "notice--warning"}`} role="status">{t("providers.testResult", { status: probeResult.status, time: new Date(probeResult.checked_at).toLocaleString() })}</div>}
  </>;
}
