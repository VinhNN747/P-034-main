"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useLanguage } from "@/components/language-provider";

import {
  createBillingTopup,
  getBillingAccount,
  getBillingSettings,
  getBillingTransactions,
  GatewayApiError,
  updateBillingSettings,
} from "@/lib/gateway-api";
import type { BillingAccount, BillingSettings, BillingTransaction } from "@/lib/gateway-types";
import { PaginationControls, TABLE_PAGE_SIZE } from "@/components/gateway/pagination-controls";
import { BillingSkeleton } from "@/components/gateway/gateway-page-skeletons";

function message(error: unknown, fallback: string): string {
  return error && typeof error === "object" && "message" in error
    ? String((error as GatewayApiError).message)
    : fallback;
}

function State({ text, error = false }: { text: string; error?: boolean }) {
  return <div className={error ? "notice notice--warning" : "notice"} role="status">{text}</div>;
}

export function BillingScene({ permissions }: { permissions: readonly string[] }) {
  const { t } = useLanguage();
  const writable = permissions.includes("billing.manage");
  const [account, setAccount] = useState<BillingAccount | null>(null);
  const [settings, setSettings] = useState<BillingSettings | null>(null);
  const [transactions, setTransactions] = useState<BillingTransaction[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const pageSize = TABLE_PAGE_SIZE;
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [threshold, setThreshold] = useState("10");
  const [notify, setNotify] = useState(true);

  async function load() {
    try {
      const [nextAccount, nextSettings, nextTransactions] = await Promise.all([
        getBillingAccount(),
        getBillingSettings(),
        getBillingTransactions({ page, page_size: pageSize }),
      ]);
      setAccount(nextAccount);
      setSettings(nextSettings);
      setTransactions(nextTransactions.items);
      setTotal(nextTransactions.total);
      setThreshold(String(nextSettings.low_balance_threshold));
      setNotify(nextSettings.notify_low_balance);
      setState("ready");
      setError(null);
    } catch (reason: unknown) {
      setState("error");
      setError(message(reason, t("billing.loadError")));
    }
  }

  useEffect(() => {
    let active = true;
    void Promise.all([getBillingAccount(), getBillingSettings(), getBillingTransactions({ page, page_size: pageSize })]).then(([nextAccount, nextSettings, nextTransactions]) => {
      if (!active) return;
      setAccount(nextAccount);
      setSettings(nextSettings);
      setTransactions(nextTransactions.items);
      setTotal(nextTransactions.total);
      setThreshold(String(nextSettings.low_balance_threshold));
      setNotify(nextSettings.notify_low_balance);
      setState("ready");
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("error");
      setError(message(reason, t("billing.loadError")));
    });
    return () => { active = false; };
  }, [page, pageSize, t]);

  async function topUp() {
    setSaving(true);
    try {
      const key = `fe-topup-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const result = await createBillingTopup({ amount: 2, currency: "USD", idempotency_key: key });
      setAccount((current) => current ? { ...current, simulated_topup_used: true } : current);
      setNotice(t("billing.creditAdded", { amount: result.amount.toFixed(2), currency: result.currency }));
      await load();
    } catch (reason: unknown) {
      if (reason instanceof GatewayApiError && reason.status === 409) {
        await load();
        return;
      }
      setError(message(reason, t("billing.creditError")));
    } finally {
      setSaving(false);
    }
  }

  async function saveSettings(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    try {
      const next = await updateBillingSettings({ low_balance_threshold: Number(threshold), notify_low_balance: notify, auto_topup_enabled: settings?.auto_topup_enabled ?? false });
      setSettings(next);
      setNotice(t("billing.settingsSaved"));
    } catch (reason: unknown) {
      setError(message(reason, t("billing.settingsError")));
    } finally {
      setSaving(false);
    }
  }

  if (state === "loading") return <BillingSkeleton label={t("billing.localLedger")} />;
  if (state === "error" && !account) return <State text={error ?? t("billing.localLedger")} error />;
  if (!account) return <State text={t("billing.noTransactions")} />;

  return <div className="billing-page">
    <section className="gateway-page-heading billing-page-heading">
      <div>
        <p className="gateway-eyebrow">{t("billing.localLedger")}</p>
        <h1>{t("billing.title")}</h1>
        <p>{t("billing.description")}</p>
      </div>
      <div className="billing-topup-action">
        <button className="gateway-button" type="button" disabled={!writable || saving || account.simulated_topup_used} onClick={() => void topUp()}>
          {saving ? t("billing.saving") : t("billing.addCredit")}
        </button>
        {account.simulated_topup_used && <small className="billing-topup-limit" role="status">{t("billing.topupUsedNotice")}</small>}
      </div>
    </section>
    {!writable && <div className="notice notice--warning">{t("billing.readOnly")}</div>}
    {notice && <div className="notice notice--success" role="status">{notice}</div>}
    {error && <State text={error} error />}
    <section className="billing-overview">
      <section className="billing-balance-card">
        <div className="billing-card-topline"><p className="gateway-eyebrow">{t("billing.balance")}</p><span className="gateway-status gateway-status--healthy">{t("billing.local")}</span></div>
        <div className="billing-balance"><strong>{account.balance.toFixed(2)}</strong><span>{account.currency}</span></div>
        <p className="billing-rate-note">{t("billing.runtimeNote")}</p>
      </section>
      <div className="billing-quick-stats">
        <article><span>{t("billing.lifetimeTopup")}</span><strong>{account.lifetime_topup.toFixed(2)}</strong><small>{account.currency} {t("billing.simulatedCredit")}</small></article>
        <article><span>{t("billing.lifetimeUsage")}</span><strong>{account.lifetime_usage.toFixed(2)}</strong><small>{account.currency} {t("billing.runtimeDebit")}</small></article>
        <article><span>{t("billing.entries")}</span><strong>{total}</strong><small>{t("billing.immutableRecords")}</small></article>
      </div>
    </section>
    <div className="billing-detail-grid">
      <section className="gateway-panel billing-transactions-panel">
        <div className="gateway-section-heading">
          <div><p className="gateway-eyebrow">{t("billing.transactionHistory")}</p><h2>{t("billing.localLedgerTitle")}</h2></div>
          <Link href="/usage">{t("billing.openUsage")}</Link>
        </div>
        {transactions.length === 0 ? <State text={t("billing.noTransactions")} /> : <>
          <p className="billing-table-hint">{t("billing.tableHint")}</p>
          <div className="gateway-table-wrap" tabIndex={0} role="region" aria-label={t("billing.details")}>
            <table className="gateway-table billing-table">
              <thead><tr><th>{t("billing.type")}</th><th className="billing-amount-column">{t("billing.amount")}</th><th>{t("billing.status")}</th><th>{t("billing.reference")}</th><th>{t("billing.timestamp")}</th></tr></thead>
              <tbody>{transactions.map((transaction) => <tr key={transaction.id}>
                <td><strong>{transaction.type}</strong><small>{transaction.simulation ? t("billing.simulated") : transaction.description}</small></td>
                <td className="numeric billing-amount">{transaction.amount.toFixed(2)} {transaction.currency}</td>
                <td>{transaction.status}</td>
                <td className="billing-reference">{transaction.reference ?? "—"}</td>
                <td className="billing-timestamp">{new Date(transaction.timestamp).toLocaleString()}</td>
              </tr>)}</tbody>
            </table>
          </div>
          <PaginationControls page={page} pageSize={pageSize} total={total} onPageChange={setPage} label={t("billing.pagination")} />
        </>}
      </section>
      <form className="gateway-panel form-panel billing-settings-panel" onSubmit={saveSettings}>
        <div className="form-section">
          <div>
            <p className="gateway-eyebrow">{t("billing.notifications")}</p>
            <h2>{t("billing.settings")}</h2>
            <p>{t("billing.settingsDescription")}</p>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>{t("billing.threshold")}</span>
              <input type="number" min="0.000001" step="0.000001" value={threshold} onChange={(event) => setThreshold(event.target.value)} disabled={!writable} />
            </label>
            <label className="switch-row">
              <input type="checkbox" checked={notify} onChange={(event) => setNotify(event.target.checked)} disabled={!writable} />
              <span><strong>{t("billing.notify")}</strong><small>{t("billing.notifyDescription")}</small></span>
            </label>
          </div>
        </div>
        <div className="form-actions"><button type="submit" className="gateway-button" disabled={!writable || saving}>{t("billing.saveSettings")}</button></div>
      </form>
    </div>
  </div>;
}
