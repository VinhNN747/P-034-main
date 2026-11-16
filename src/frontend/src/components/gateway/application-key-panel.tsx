"use client";

import { useEffect, useState } from "react";
import { useLanguage } from "@/components/language-provider";

import {
  createApplicationKey,
  listApplicationKeys,
  revokeApplicationKey,
  type GatewayApiError,
} from "@/lib/gateway-api";
import type { ApplicationKey } from "@/lib/gateway-types";
import { OneTimeApiKeySecret } from "@/components/gateway/api-key-secret";
import { PaginationControls, TABLE_PAGE_SIZE } from "@/components/gateway/pagination-controls";

function apiMessage(error: unknown, fallback: string): string {
  return error && typeof error === "object" && "message" in error
    ? String((error as GatewayApiError).message)
    : fallback;
}

export function ApplicationKeyPanel({ applicationId, canManage }: { applicationId: string; canManage: boolean }) {
  const { t } = useLanguage();
  const [items, setItems] = useState<ApplicationKey[]>([]);
  const [name, setName] = useState("");
  const [rawKey, setRawKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const pageSize = TABLE_PAGE_SIZE;

  useEffect(() => {
    let active = true;
    void listApplicationKeys(applicationId, { page, page_size: pageSize }).then((response) => {
      if (active) {
        setItems(response.items);
        setTotal(response.total);
      }
    }).catch((reason: unknown) => {
      if (active) setError(apiMessage(reason, t("keys.loadError")));
    });
    return () => { active = false; };
  }, [applicationId, page, pageSize, t]);

  async function createKey(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createApplicationKey(applicationId, { name: name.trim() });
      setRawKey(created.raw_key);
      setName("");
      setTotal((current) => current + 1);
      if (page === 1) {
        const metadata: ApplicationKey = {
          id: created.id,
          name: created.name,
          key_prefix: created.key_prefix,
          created_at: created.created_at,
          last_used_at: created.last_used_at,
          revoked_at: created.revoked_at,
          revoked: created.revoked,
        };
        setItems((current) => [metadata, ...current].slice(0, pageSize));
      } else {
        setPage(1);
      }
    } catch (reason: unknown) {
      setError(apiMessage(reason, t("keys.actionError")));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(key: ApplicationKey) {
    setBusy(true);
    try {
      const revoked = await revokeApplicationKey(applicationId, key.id);
      setItems((current) => current.map((item) => item.id === revoked.id ? revoked : item));
    } catch (reason: unknown) {
      setError(apiMessage(reason, t("keys.actionError")));
    } finally {
      setBusy(false);
    }
  }

  return <section className="gateway-panel form-panel"><div className="form-section"><div><p className="gateway-eyebrow">{t("keys.credentials")}</p><h2>{t("keys.title")}</h2><p>{t("keys.description")}</p></div><div>{rawKey && <OneTimeApiKeySecret rawKey={rawKey} />}{error && <div className="notice notice--warning" role="status">{error}</div>}{items.length > 0 && <><div className="gateway-table-wrap"><table className="gateway-table"><thead><tr><th>{t("keys.name")}</th><th>{t("keys.prefix")}</th><th>{t("keys.created")}</th><th>{t("keys.status")}</th><th /></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td>{item.name}</td><td><code>{item.key_prefix}…</code></td><td>{new Date(item.created_at).toLocaleDateString()}</td><td>{item.revoked ? t("keys.revoked") : t("keys.active")}</td><td>{canManage && !item.revoked && <button type="button" className="gateway-text-button" disabled={busy} onClick={() => void revoke(item)}>{t("keys.revoke")}</button>}</td></tr>)}</tbody></table></div><PaginationControls page={page} pageSize={pageSize} total={total} onPageChange={setPage} label={t("keys.pagination")} /></>}{canManage && <form className="form-actions" onSubmit={createKey}><input className="field" aria-label={t("keys.keyName")} value={name} onChange={(event) => setName(event.target.value)} placeholder={t("keys.keyName")} /><button type="submit" className="gateway-button" disabled={busy || !name.trim()}>{busy ? t("common.saving") : t("keys.create")}</button></form>}</div></div></section>;
}
