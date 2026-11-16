"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useLanguage } from "@/components/language-provider";
import { createClient } from "@/lib/supabase/client";
import { listIncidentReports } from "@/lib/gateway-api";
import type { Incident, IncidentStatus } from "@/lib/gateway-types";

const POLL_INTERVAL_MS = 60_000;
const REPORT_PAGE_SIZE = 100;

function readTimestamp(key: string): number | null {
  try {
    const value = window.localStorage.getItem(key);
    if (value === null) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function writeTimestamp(key: string, value: number) {
  try {
    window.localStorage.setItem(key, String(value));
  } catch {
    // Notifications remain usable when browser storage is unavailable.
  }
}

function latestTimestamp(items: Incident[]) {
  return items.reduce((latest, item) => {
    const timestamp = Date.parse(item.updated_at);
    return Number.isFinite(timestamp) ? Math.max(latest, timestamp) : latest;
  }, 0);
}

function formattedDate(value: string, locale: string) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function NotificationCenter() {
  const { language, t } = useLanguage();
  const [userId, setUserId] = useState<string | null>(null);
  const [reports, setReports] = useState<Incident[]>([]);
  const [lastSeen, setLastSeen] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [open, setOpen] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const baselineKeyRef = useRef<string | null>(null);
  const storageKey = userId ? `taskway:report-notifications:${userId}:read-through` : null;

  useEffect(() => {
    let active = true;
    void createClient().auth.getSession().then(({ data, error }) => {
      if (!active) return;
      if (error || !data.session?.user.id) return;
      setUserId(data.session.user.id);
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!userId || !storageKey) return;
    const notificationStorageKey = storageKey;
    let active = true;

    async function load(silent = false) {
      if (!silent) setLoading(true);
      try {
        const response = await listIncidentReports({ page: 1, page_size: REPORT_PAGE_SIZE });
        if (!active) return;
        const orderedReports = [...response.items].sort(
          (left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at),
        );
        setReports(orderedReports);
        setLoadError(false);
        if (baselineKeyRef.current !== storageKey) {
          const storedTimestamp = readTimestamp(notificationStorageKey);
          if (storedTimestamp !== null) {
            setLastSeen(storedTimestamp);
            baselineKeyRef.current = storageKey;
            return;
          }
          // Treat reports already present on first visit as history; only later
          // admin updates should arrive as unread notifications.
          const baseline = latestTimestamp(orderedReports) || Date.now();
          writeTimestamp(notificationStorageKey, baseline);
          setLastSeen(baseline);
          baselineKeyRef.current = storageKey;
        }
      } catch {
        if (active) setLoadError(true);
      } finally {
        if (active && !silent) setLoading(false);
      }
    }

    void load();
    const interval = window.setInterval(() => void load(true), POLL_INTERVAL_MS);
    function reloadWhenVisible() {
      if (document.visibilityState === "visible") void load(true);
    }
    document.addEventListener("visibilitychange", reloadWhenVisible);
    return () => {
      active = false;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", reloadWhenVisible);
    };
  }, [refreshVersion, storageKey, userId]);

  const unreadReports = useMemo(() => reports.filter((report) => {
    const updatedAt = Date.parse(report.updated_at);
    const createdAt = Date.parse(report.opened_at);
    return lastSeen !== null
      && Number.isFinite(updatedAt)
      && Number.isFinite(createdAt)
      && updatedAt > lastSeen
      && updatedAt - createdAt > 1_000;
  }), [lastSeen, reports]);
  const markVisibleAsRead = useCallback(() => {
    if (!storageKey || unreadReports.length === 0) return;
    const timestamp = latestTimestamp(unreadReports);
    writeTimestamp(storageKey, timestamp);
    setLastSeen(timestamp);
  }, [storageKey, unreadReports]);

  const closeMenu = useCallback(() => {
    markVisibleAsRead();
    setOpen(false);
  }, [markVisibleAsRead]);

  useEffect(() => {
    if (!open) return;
    function closeOnPointerDown(event: PointerEvent) {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) closeMenu();
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") closeMenu();
    }
    document.addEventListener("pointerdown", closeOnPointerDown);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnPointerDown);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [closeMenu, open]);

  const statusLabels: Record<IncidentStatus, string> = {
    investigating: t("incidents.investigating"),
    monitoring: t("incidents.monitoring"),
    resolved: t("incidents.resolved"),
  };

  return (
    <div className="workspace-notifications" ref={rootRef}>
      <button
        type="button"
        className="workspace-notification-trigger"
        aria-label={unreadReports.length ? t("notifications.unreadCount", { count: unreadReports.length }) : t("notifications.label")}
        title={t("notifications.label")}
        aria-expanded={open}
        aria-controls="workspace-notifications-panel"
        onClick={() => open ? closeMenu() : setOpen(true)}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></svg>
        {unreadReports.length > 0 && <span className="workspace-notification-badge" aria-hidden="true">{unreadReports.length > 9 ? "9+" : unreadReports.length}</span>}
        <span className="workspace-visually-hidden" aria-live="polite">{unreadReports.length}</span>
      </button>
      {open && <section className="workspace-notification-panel" id="workspace-notifications-panel" aria-label={t("notifications.title")}>
        <header className="workspace-notification-heading">
          <h2>{t("notifications.title")}</h2>
          <button type="button" className="workspace-notification-refresh" onClick={() => setRefreshVersion((value) => value + 1)} aria-label={t("notifications.refresh")} title={t("notifications.refresh")}>
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16 8a6.2 6.2 0 0 0-10.7-2L4 7.5M4 4v3.5h3.5M4 12a6.2 6.2 0 0 0 10.7 2L16 12.5M16 16v-3.5h-3.5" /></svg>
          </button>
        </header>
        {loading && <p className="workspace-notification-state">{t("common.loading")}</p>}
        {loadError && !loading && <p className="workspace-notification-error" role="alert">{t("notifications.loadError")}</p>}
        {!loading && !loadError && unreadReports.length === 0 && <p className="workspace-notification-state">{t("notifications.empty")}</p>}
        {unreadReports.length > 0 && <div className="workspace-notification-list">
          {unreadReports.map((report) => <article className="workspace-notification-item" key={report.id}>
            <span className="workspace-notification-item-dot" aria-hidden="true" />
            <div>
              <strong>{report.title}</strong>
              <p>{t("notifications.reportUpdated")} {t("notifications.reportStatus", { status: statusLabels[report.status] })}</p>
              {report.resolution_note && <p className="workspace-notification-note">{report.resolution_note}</p>}
              <time dateTime={report.updated_at}>{formattedDate(report.updated_at, language === "vi" ? "vi-VN" : "en-US")}</time>
            </div>
          </article>)}
        </div>}
        <Link className="workspace-notification-history" href="/reports" onClick={closeMenu}>{t("notifications.viewReports")}</Link>
      </section>}
    </div>
  );
}
