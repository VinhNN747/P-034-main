"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname } from "next/navigation";

import { useWorkspace } from "@/components/gateway/workspace-provider";
import { useLanguage } from "@/components/language-provider";
import { SupportChatHistory, type ChatSession } from "@/components/support-chat-history";

import { SupportAnalytics, type SupportData } from "@/components/support-analytics";
import { SupportChatChoices, type SupportChoice } from "@/components/support-chat-choices";
import { TaskwayAssistantMark } from "@/components/taskway-assistant-mark";
import { SupportMessageContent } from "@/components/support-message-content";
import { SupportChatReferences } from "@/components/support-chat-references";

type Source = { id: string; title: string; url?: string | null };
type Action = { label: string; url: string };
type Message = { id: string; role: "user" | "assistant"; content: string; sources?: Source[]; actions?: Action[]; choices?: SupportChoice[]; data?: SupportData | null };
type SessionPage = { items: ChatSession[]; has_more: boolean };
type SessionDetail = { session: ChatSession; messages: Message[]; has_more: boolean };
type Answer = { session_id: string; message: string; sources?: Source[]; actions?: Action[]; choices?: SupportChoice[]; suggestions?: string[]; data?: SupportData | null };
type ChatContext = { page: string; application_id?: string };

function pageContext(pathname: string): ChatContext {
  const match = /^\/applications\/([^/]+)(?:\/(settings|playground))?$/.exec(pathname);
  if (match && match[1] !== "new") {
    return {
      page: match[2] === "settings" ? "application_settings" : match[2] === "playground" ? "playground" : "application_detail",
      application_id: match[1],
    };
  }
  if (pathname === "/applications") return { page: "applications" };
  if (pathname === "/usage") return { page: "usage" };
  if (pathname === "/billing") return { page: "billing" };
  if (pathname === "/available-models" || pathname === "/models") return { page: "models" };
  return { page: "workspace" };
}

function welcomeMessage(content: string): Message {
  return { id: "welcome", role: "assistant", content };
}

async function parseResponse<T>(response: Response): Promise<T> {
  const payload = await response.json() as T & { error?: string; detail?: string };
  if (!response.ok) throw new Error(payload.error ?? payload.detail ?? "The assistant did not respond.");
  return payload;
}

export function BetaChatWidget() {
  const { userId, authenticationComplete, roles, permissions, status } = useWorkspace();
  const pathname = usePathname();
  const authenticationPage = ["/login", "/sign-up", "/forgot-password", "/update-password", "/auth"].some(
    (path) => pathname === path || pathname.startsWith(`${path}/`),
  );
  if (pathname === "/" || authenticationPage || !userId || !authenticationComplete || status !== "ready" || !roles.includes("user") || roles.includes("admin") || roles.includes("platform_admin")
    || !permissions.includes("applications.read_own")) return null;
  return <AuthenticatedBetaChat key={userId} userId={userId} permissions={permissions} />;
}

function AuthenticatedBetaChat({ userId, permissions }: { userId: string; permissions: string[] }) {
  const pathname = usePathname();
  const { t, language } = useLanguage();
  const context = useMemo(() => pageContext(pathname), [pathname]);
  const chatFetch = useCallback((url: string, options: RequestInit = {}) => {
    const headers = new Headers(options.headers);
    headers.set("X-Workspace-User", userId);
    return fetch(url, { ...options, headers, cache: "no-store" });
  }, [userId]);
  const welcome = useMemo(() => {
    if (language === "vi" || language === "en") return welcomeMessage(t("chat.welcome"));
    return welcomeMessage(
    language === "vi"
      ? "Mình có thể giải thích Gateway, hướng dẫn về applications, API keys, models và routing, hoặc giúp bạn xem Requests, Usage và Billing theo quyền truy cập. Bạn muốn tìm hiểu điều gì?"
      : "I can explain how the Gateway works, guide you through applications, API keys, models and routing, or help you explore Requests, Usage and Billing based on your access. What would you like to know?",
    );
  }, [language, t]);
  const documentationSuggestions = useMemo(() => [
    t("chat.defaultHowToUse"),
    t("chat.defaultAuthentication"),
    t("chat.defaultReadOutput"),
  ], [t]);
  const defaultSuggestions = useMemo(() => [...[
    t("chat.defaultListApplications"),
    ...(permissions.includes("usage.read_own")
      ? [context.application_id ? t("chat.defaultChartApplicationCost") : t("chat.defaultChartCost")]
      : []),
    ...(permissions.includes("billing.read") ? [t("chat.defaultCreditBalance")] : []),
    t("chat.defaultCreateApiKey"),
  ].slice(0, 3), ...documentationSuggestions], [permissions, context.application_id, t, documentationSuggestions]);
  const [open, setOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [hasMoreSessions, setHasMoreSessions] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([welcome]);
  const [hasMoreMessages, setHasMoreMessages] = useState(false);
  const [messageOffset, setMessageOffset] = useState(0);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const nextMessageId = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const historyButtonRef = useRef<HTMLButtonElement>(null);
  const welcomeRef = useRef(welcome);
  const tRef = useRef(t);
  const showIntro = messages.length === 1 && messages[0]?.id === "welcome";
  const displayMessages = useMemo(() => messages.map((message) => message.id === "welcome" ? welcome : message), [messages, welcome]);

  useEffect(() => {
    welcomeRef.current = welcome;
    tRef.current = t;
  }, [welcome, t]);

  useEffect(() => {
    if (!open) return;
    const focusInput = window.setTimeout(() => { if (!historyOpen) inputRef.current?.focus(); }, 160);
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        if (historyOpen) {
          setHistoryOpen(false);
          historyButtonRef.current?.focus();
        } else {
          setOpen(false);
        }
      }
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.clearTimeout(focusInput);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [open, historyOpen]);

  useEffect(() => {
    let active = true;
    async function restore() {
      try {
        const page = await parseResponse<SessionPage>(await chatFetch("/api/beta-chat/sessions"));
        if (!active) return;
        setSessions(page.items);
        setHasMoreSessions(page.has_more);
        if (page.items[0]) {
          const detail = await parseResponse<SessionDetail>(
            await chatFetch(`/api/beta-chat/sessions/${encodeURIComponent(page.items[0].id)}`),
          );
          if (!active) return;
          setSessionId(detail.session.id);
          setMessages(detail.messages.length ? detail.messages : [welcomeRef.current]);
          setMessageOffset(detail.messages.length);
          setHasMoreMessages(detail.has_more);
        }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : tRef.current("chat.errorLoadHistory"));
      } finally {
        if (active) setBusy(false);
      }
    }
    void restore();
    return () => { active = false; };
  }, [chatFetch]);

  async function selectSession(id: string) {
    if (busy || loading) return;
    setBusy(true);
    setError(null);
    try {
      const detail = await parseResponse<SessionDetail>(
        await chatFetch(`/api/beta-chat/sessions/${encodeURIComponent(id)}`),
      );
      setSessionId(detail.session.id);
      setMessages(detail.messages.length ? detail.messages : [welcome]);
      setMessageOffset(detail.messages.length);
      setHasMoreMessages(detail.has_more);
      setSuggestions([]);
      setHistoryOpen(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("chat.errorOpenConversation"));
    } finally {
      setBusy(false);
    }
  }

  async function createNewSession() {
    if (busy || loading) return;
    setBusy(true);
    setError(null);
    try {
      const created = await parseResponse<ChatSession>(await chatFetch("/api/beta-chat/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }));
      setSessions((current) => [created, ...current]);
      setSessionId(created.id);
      setMessages([welcome]);
      setMessageOffset(0);
      setHasMoreMessages(false);
      setSuggestions([]);
      setDraft("");
      setHistoryOpen(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("chat.errorCreateConversation"));
    } finally {
      setBusy(false);
    }
  }

  async function deleteSession(id: string): Promise<boolean> {
    if (busy || loading) return false;
    setBusy(true);
    setError(null);
    try {
      const response = await chatFetch(`/api/beta-chat/sessions/${encodeURIComponent(id)}`, {
        method: "DELETE", cache: "no-store",
      });
      // A session deleted from another tab is already in the requested state.
      if (response.status !== 204 && response.status !== 404) {
        await parseResponse(response);
        throw new Error(t("chat.errorDeleteConversation"));
      }
      setSessions((current) => current.filter((item) => item.id !== id));
      if (sessionId === id) {
        setSessionId(null);
        setMessages([welcome]);
        setMessageOffset(0);
        setHasMoreMessages(false);
        setSuggestions([]);
        setDraft("");
      }
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("chat.errorDeleteConversation"));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function updateSession(id: string, update: { title?: string; pinned?: boolean }): Promise<boolean> {
    if (busy || loading) return false;
    setBusy(true);
    setError(null);
    try {
      const updated = await parseResponse<ChatSession>(await chatFetch(`/api/beta-chat/sessions/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(update),
        cache: "no-store",
      }));
      setSessions((current) => current.map((item) => item.id === id ? updated : item));
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("chat.errorUpdateConversation"));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function loadMoreSessions() {
    if (busy || loading || !hasMoreSessions) return;
    setBusy(true);
    setError(null);
    try {
      const page = await parseResponse<SessionPage>(
        await chatFetch(`/api/beta-chat/sessions?offset=${sessions.length}&limit=30`),
      );
      setSessions((current) => [...current, ...page.items.filter((item) => !current.some((old) => old.id === item.id))]);
      setHasMoreSessions(page.has_more);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("chat.errorLoadMoreConversations"));
    } finally {
      setBusy(false);
    }
  }

  async function loadOlderMessages() {
    if (busy || loading || !sessionId || !hasMoreMessages) return;
    setBusy(true);
    try {
      const detail = await parseResponse<SessionDetail>(
        await chatFetch(`/api/beta-chat/sessions/${encodeURIComponent(sessionId)}?offset=${messageOffset}&limit=100`),
      );
      setMessages((current) => [...detail.messages, ...current]);
      setMessageOffset((current) => current + detail.messages.length);
      setHasMoreMessages(detail.has_more);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("chat.errorLoadOlderMessages"));
    } finally {
      setBusy(false);
    }
  }

  async function submitContent(content: string) {
    if (!content || busy || loading) return;
    const pendingId = `pending-${nextMessageId.current++}`;
    setMessages((current) => [
      ...current.filter((message) => message.id !== "welcome"),
      { id: pendingId, role: "user", content },
    ]);
    setDraft("");
    setError(null);
    setLoading(true);
    try {
      const answer = await parseResponse<Answer>(await chatFetch("/api/beta-chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: content, context, session_id: sessionId }),
      }));
      if (!answer.message || !answer.session_id) throw new Error(t("chat.errorInvalidResponse"));
      setSessionId(answer.session_id);
      setMessages((current) => [...current, {
        id: `answer-${nextMessageId.current++}`,
        role: "assistant",
        content: answer.message,
        sources: answer.sources,
        actions: answer.actions,
        choices: answer.choices,
        data: answer.data,
      }]);
      setMessageOffset((current) => current + 2);
      setSuggestions(answer.suggestions ?? []);
      try {
        const page = await parseResponse<SessionPage>(await chatFetch("/api/beta-chat/sessions"));
        setSessions(page.items);
        setHasMoreSessions(page.has_more);
      } catch {
        // The answer is already saved; a later history refresh can update the list.
      }
    } catch (cause) {
      setMessages((current) => current.filter((message) => message.id !== pendingId));
      setDraft(content);
      setError(cause instanceof Error ? cause.message : t("chat.errorNoResponse"));
    } finally {
      setLoading(false);
    }
  }

  function sendMessage(event: FormEvent) {
    event.preventDefault();
    void submitContent(draft.trim());
  }

  return <>
    {open && <section className="beta-chat-panel" aria-label={t("chat.gatewayAssistant")}>
      <header className="beta-chat-header">
        <div className="beta-chat-profile">
          <span className="beta-chat-avatar"><TaskwayAssistantMark /></span>
          <div><strong>{t("chat.gatewayAssistant")}</strong><small><i />{t("chat.accessAwareSupport")}</small></div>
        </div>
        <div className="beta-chat-header-actions">
          <button ref={historyButtonRef} type="button" aria-label={t("chat.history")} title={t("chat.history")} aria-expanded={historyOpen} aria-controls={historyOpen ? "beta-chat-history" : undefined} onClick={() => setHistoryOpen((current) => !current)} disabled={busy || loading}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h10" /></svg>
          </button>
          <button type="button" aria-label={t("common.newConversation")} title={t("common.newConversation")} onClick={() => void createNewSession()} disabled={busy || loading}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
          </button>
          <button type="button" className="beta-chat-close" aria-label={t("chat.closeAssistant")} title={t("chat.closeAssistant")} onClick={() => setOpen(false)}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </div>
      </header>
      {historyOpen ? <SupportChatHistory
        sessions={sessions}
        activeSessionId={sessionId}
        busy={busy || loading}
        hasMore={hasMoreSessions}
        error={error}
        onSelect={(id) => void selectSession(id)}
        onRename={(id, title) => updateSession(id, { title })}
        onTogglePin={(id, pinned) => updateSession(id, { pinned })}
        onDelete={deleteSession}
        onCreate={() => void createNewSession()}
        onLoadMore={() => void loadMoreSessions()}
        onClose={() => { setHistoryOpen(false); historyButtonRef.current?.focus(); }}
      /> : <>
      {showIntro && <div className="beta-chat-intro">
        <div><span className="beta-chat-eyebrow">{t("chat.eyebrow")}</span><p>{t("chat.hello")} <span aria-hidden="true">👋</span></p><strong>{t("chat.howCanIHelp")}</strong><small>{context.application_id ? t("chat.currentApplicationContext") : t("chat.exploreDescription")}</small></div>
        <span className="beta-chat-status"><i />{t("chat.ready")}</span>
      </div>}
      <div className="beta-chat-messages" aria-live="polite">
        {hasMoreMessages && <button type="button" className="beta-chat-load-older" onClick={() => void loadOlderMessages()} disabled={busy || loading}>{t("chat.loadOlderMessages")}</button>}
        {displayMessages.map((message) => <div key={message.id} className={`beta-chat-message beta-chat-message--${message.role}${message.data ? " beta-chat-message--analytics" : ""}`}>
          {message.role === "assistant" ? <SupportMessageContent content={message.content} /> : <span>{message.content}</span>}
          {message.role === "assistant" && message.data && <SupportAnalytics key={message.data.id} data={message.data} />}
          {message.role === "assistant" && message.choices?.length ? <SupportChatChoices
            choices={message.choices}
            disabled={loading || busy || message.id !== displayMessages.at(-1)?.id}
            onSelect={(content) => void submitContent(content)}
          /> : null}
          {message.id === "welcome" && <small>{t("chat.gatewayAssistant")}</small>}
          {message.role === "assistant" && <SupportChatReferences sources={message.sources} actions={message.actions} />}
        </div>)}
        {loading && <div className="beta-chat-message beta-chat-message--assistant"><span className="beta-chat-typing">● ● ●</span></div>}
        {error && <p className="beta-chat-error" role="alert">{error}</p>}
        {!loading && !busy && !displayMessages.at(-1)?.choices?.length && <div className="beta-chat-suggestions">
          {Array.from(new Set(suggestions.length ? [...suggestions, ...documentationSuggestions] : defaultSuggestions)).map((suggestion) => <button key={suggestion} type="button" onClick={() => void submitContent(suggestion)}>{suggestion}</button>)}
        </div>}
      </div>
      <form className="beta-chat-composer" onSubmit={sendMessage}>
        <input ref={inputRef} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={t("chat.askQuestion")} aria-label={t("common.message")} maxLength={5000} disabled={loading || busy} />
        <button type="submit" aria-label={t("chat.sendMessage")} disabled={loading || busy || !draft.trim()}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 14-7-4 14-3-5-7-2Z" /><path d="m12 14 3-3" /></svg></button>
      </form>
      <small className="beta-chat-note"><span>⌁</span>{t("chat.historyStored")}</small>
      </>}
    </section>}
    {!open && <button type="button" className="beta-chat-launcher" aria-label={t("chat.gatewayAssistant")} title={t("chat.gatewayAssistant")} onClick={() => setOpen(true)}><TaskwayAssistantMark /><i aria-hidden="true" /></button>}
  </>;
}
