import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { useLanguage } from "@/components/language-provider";

export type ChatSession = { id: string; title: string; pinned: boolean; updated_at: string };

type HistoryProps = {
  sessions: ChatSession[];
  activeSessionId: string | null;
  busy: boolean;
  hasMore: boolean;
  error: string | null;
  onSelect: (id: string) => void;
  onRename: (id: string, title: string) => Promise<boolean>;
  onTogglePin: (id: string, pinned: boolean) => Promise<boolean>;
  onDelete: (id: string) => Promise<boolean>;
  onCreate: () => void;
  onLoadMore: () => void;
  onClose: () => void;
};

const dateFormat = new Intl.DateTimeFormat("en-US", { day: "2-digit", month: "short" });
const timeFormat = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" });
const fullDateFormat = new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" });

function searchText(value: string) {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/đ/g, "d").replace(/Đ/g, "D").toLowerCase();
}

function sessionDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function dayNumber(date: Date) {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86_400_000;
}

export function SupportChatHistory({
  sessions, activeSessionId, busy, hasMore, error, onSelect, onRename, onTogglePin, onDelete, onCreate, onLoadMore, onClose,
}: HistoryProps) {
  const { t } = useLanguage();
  const [query, setQuery] = useState("");
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const actionButtons = useRef(new Map<string, HTMLButtonElement>());
  const renameInputRef = useRef<HTMLInputElement>(null);
  const cancelDeleteRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (pendingDeleteId) cancelDeleteRef.current?.focus();
  }, [pendingDeleteId]);

  useEffect(() => {
    if (renamingId) renameInputRef.current?.select();
  }, [renamingId]);

  useEffect(() => {
    function closeMenu(event: PointerEvent) {
      if (openMenuId && event.target instanceof Element && !event.target.closest("[data-session-actions]")) {
        setOpenMenuId(null);
      }
    }
    document.addEventListener("pointerdown", closeMenu);
    return () => document.removeEventListener("pointerdown", closeMenu);
  }, [openMenuId]);

  useEffect(() => {
    // Keep the mobile keyboard closed until the user chooses to search.
    if (window.matchMedia("(max-width: 560px)").matches) backRef.current?.focus();
    else searchRef.current?.focus();
  }, []);

  function focusActions(id: string) {
    actionButtons.current.get(id)?.focus();
  }

  function cancelDelete() {
    const id = pendingDeleteId;
    setPendingDeleteId(null);
    if (id) focusActions(id);
  }

  async function confirmDelete(id: string) {
    if (await onDelete(id)) {
      setPendingDeleteId(null);
      if (window.matchMedia("(max-width: 560px)").matches) backRef.current?.focus();
      else searchRef.current?.focus();
    }
  }

  function startRename(item: ChatSession) {
    setOpenMenuId(null);
    setPendingDeleteId(null);
    setRenameValue(item.title || t("common.newConversation"));
    setRenamingId(item.id);
  }

  function cancelRename() {
    const id = renamingId;
    setRenamingId(null);
    setRenameValue("");
    if (id) focusActions(id);
  }

  async function submitRename(event: FormEvent<HTMLFormElement>, id: string) {
    event.preventDefault();
    const title = renameValue.trim();
    if (!title) return;
    if (await onRename(id, title)) {
      setRenamingId(null);
      setRenameValue("");
      focusActions(id);
    }
  }

  const groups = useMemo(() => {
    const today = dayNumber(new Date());
    const dated = [t("history.today"), t("history.yesterday"), t("history.previous7Days"), t("history.earlier")]
      .map((label, index) => ({ label, today: index === 0, items: [] as ChatSession[] }));
    const matching = sessions.filter((session) => searchText(session.title).includes(searchText(query.trim())));
    matching.sort((a, b) => (sessionDate(b.updated_at)?.getTime() ?? 0) - (sessionDate(a.updated_at)?.getTime() ?? 0));
    const pinned = matching.filter((session) => session.pinned);
    for (const session of matching.filter((item) => !item.pinned)) {
      const date = sessionDate(session.updated_at);
      const age = date ? today - dayNumber(date) : Infinity;
      const group = age <= 0 ? 0 : age === 1 ? 1 : age < 7 ? 2 : 3;
      dated[group].items.push(session);
    }
    return [{ label: t("history.pinned"), today: false, items: pinned }, ...dated].filter((group) => group.items.length);
  }, [sessions, query, t]);

  return <section className="beta-chat-history" id="beta-chat-history" aria-label={t("history.conversations")} aria-busy={busy} onKeyDown={(event) => {
    if (event.key !== "Escape") return;
    if (pendingDeleteId) {
      event.stopPropagation();
      event.preventDefault();
      if (!busy) cancelDelete();
    } else if (renamingId) {
      event.stopPropagation();
      event.preventDefault();
      if (!busy) cancelRename();
    } else if (openMenuId) {
      event.stopPropagation();
      event.preventDefault();
      const id = openMenuId;
      setOpenMenuId(null);
      focusActions(id);
    }
  }}>
    <div className="beta-chat-history-heading">
      <div><strong>{t("history.conversations")}</strong><small>{t("history.selectToContinue")}</small></div>
      <button ref={backRef} type="button" className="beta-chat-history-back" aria-label={t("history.back")} title={t("history.back")} onClick={onClose}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 6-6 6 6 6" /></svg>
      </button>
    </div>
    <div className="beta-chat-history-tools">
      <button type="button" className="beta-chat-history-new" onClick={onCreate} disabled={busy}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        {t("common.newConversation")}
      </button>
      <label className="beta-chat-history-search">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></svg>
        <input ref={searchRef} type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("history.search")} aria-label={t("history.search")} />
      </label>
    </div>
    {error && <p className="beta-chat-error beta-chat-history-error" role="alert">{error}</p>}
    <div className="beta-chat-history-list" tabIndex={0} aria-label={t("history.conversationList")}>
      {groups.map((group) => <section className="beta-chat-history-group" key={group.label} aria-label={group.label}>
        <h3>{group.label}</h3>
        <ul>
          {group.items.map((item) => {
            const date = sessionDate(item.updated_at);
            const active = item.id === activeSessionId;
            const menuOpen = openMenuId === item.id;
            return <li key={item.id}>
              <div className={`beta-chat-history-row${active ? " active" : ""}`}>
                <button type="button" className={`beta-chat-history-item${active ? " active" : ""}`} aria-current={active ? "true" : undefined} title={item.title} onClick={() => onSelect(item.id)} disabled={busy}>
                  <span className="beta-chat-history-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 3V6a2 2 0 0 1 2-2Z" /><path d="M7 9h10M7 13h6" /></svg></span>
                  <span className="beta-chat-history-content">
                    <span className="beta-chat-history-title">{item.title || t("common.newConversation")}</span>
                    <span className="beta-chat-history-meta">
                      {item.pinned && <span className="beta-chat-history-pinned"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 4 6 0 1 6 3 3H5l3-3 1-6ZM12 13v7" /></svg>{t("history.pinned")}</span>}
                      {date && <time dateTime={item.updated_at} title={fullDateFormat.format(date)}>{group.today ? timeFormat.format(date) : dateFormat.format(date)}</time>}
                      {active && <span className="beta-chat-history-current"><i aria-hidden="true" />{t("history.open")}</span>}
                    </span>
                  </span>
                </button>
                <div className="beta-chat-history-actions" data-session-actions>
                  <button
                    ref={(node) => { if (node) actionButtons.current.set(item.id, node); else actionButtons.current.delete(item.id); }}
                    type="button"
                    className="beta-chat-history-menu-button"
                    aria-label={`${t("history.options")}: ${item.title || t("common.newConversation")}`}
                    title={t("history.options")}
                    aria-haspopup="menu"
                    aria-expanded={menuOpen}
                    disabled={busy}
                    onClick={() => setOpenMenuId((current) => current === item.id ? null : item.id)}
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.4" /><circle cx="12" cy="12" r="1.4" /><circle cx="19" cy="12" r="1.4" /></svg>
                  </button>
                  {menuOpen && <div className="beta-chat-history-menu" role="menu" aria-label={`${t("history.options")}: ${item.title || t("common.newConversation")}`}>
                    <button type="button" role="menuitem" onClick={() => startRename(item)}>
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 16-1 5 5-1L19 9l-4-4L4 16ZM13 7l4 4" /></svg>{t("history.rename")}
                    </button>
                    <button type="button" role="menuitem" onClick={() => { setOpenMenuId(null); void onTogglePin(item.id, !item.pinned); }}>
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 4 6 0 1 6 3 3H5l3-3 1-6ZM12 13v7" /></svg>{item.pinned ? t("history.unpin") : t("history.pin")}
                    </button>
                    <button type="button" role="menuitem" className="danger" onClick={() => { setOpenMenuId(null); setRenamingId(null); setPendingDeleteId(item.id); }}>
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6M14 10v6" /></svg>{t("common.delete")}
                    </button>
                  </div>}
                </div>
              </div>
              {renamingId === item.id && <form className="beta-chat-history-rename" onSubmit={(event) => void submitRename(event, item.id)}>
                <label htmlFor={`rename-${item.id}`}>{t("history.renameConversation")}</label>
                <input ref={renameInputRef} id={`rename-${item.id}`} value={renameValue} onChange={(event) => setRenameValue(event.target.value)} maxLength={120} disabled={busy} />
                <div>
                  <button type="button" onClick={cancelRename} disabled={busy}>{t("common.cancel")}</button>
                  <button type="submit" className="beta-chat-history-save" disabled={busy || !renameValue.trim()}>{busy ? t("common.saving") : t("common.save")}</button>
                </div>
              </form>}
              {pendingDeleteId === item.id && <div className="beta-chat-history-delete-confirm" role="group" aria-label={t("history.confirmDelete")}>
                <strong>{t("history.deleteConversation")}</strong>
                <p>{t("history.deleteDescription")}</p>
                <div>
                  <button ref={cancelDeleteRef} type="button" onClick={cancelDelete} disabled={busy}>{t("common.cancel")}</button>
                  <button type="button" className="beta-chat-history-delete-accept" onClick={() => void confirmDelete(item.id)} disabled={busy}>{busy ? t("common.deleting") : t("common.delete")}</button>
                </div>
              </div>}
            </li>;
          })}
        </ul>
      </section>)}
      {groups.length === 0 && <div className="beta-chat-history-empty" role="status">
        <strong>{busy ? t("common.loading") : query.trim() ? t("history.noResults") : t("history.noConversations")}</strong>
        <p>{query.trim() ? t("history.tryAnotherSearch") : t("history.startNew")}</p>
      </div>}
      {hasMore && <button type="button" className="beta-chat-history-more" onClick={onLoadMore} disabled={busy}>{busy ? t("common.loading") : t("history.loadMore")}</button>}
    </div>
    <small className="beta-chat-history-footer">{busy ? t("common.loading") : t("history.private")}</small>
  </section>;
}
