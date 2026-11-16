"use client";

import Link from "next/link";
import { useLanguage } from "@/components/language-provider";
import { supportLinkKind } from "@/lib/taskway-docs";

type Source = { id: string; title: string; url?: string | null };
type Action = { id?: string; label: string; url: string; onActivate?: () => void };

export function SupportChatReferences({ sources = [], actions = [] }: { sources?: Source[]; actions?: Action[] }) {
  const { t } = useLanguage();
  const related = [...sources, ...actions.filter(action => supportLinkKind(action.url) === "documentation").map(action => ({ id: action.url, title: action.label, url: action.url }))]
    .filter((source, index, all) => all.findIndex(item => source.url ? item.url === source.url : item.id === source.id) === index);
  const navigation = actions.filter(action => supportLinkKind(action.url) === "local")
    .filter((action, index, all) => all.findIndex(item => (item.id ?? item.url) === (action.id ?? action.url)) === index);
  if (!related.length && !navigation.length) return null;
  return <div className="support-chat-resources">
    {!!related.length && <details className="support-related-links">
      <summary><span aria-hidden="true">›</span><strong>{t("chat.relatedLinks")}</strong><small>{related.length}</small></summary>
      <ul>{related.map(source => <li key={source.id}>{supportLinkKind(source.url) === "documentation"
        ? <a href={source.url!} target="_blank" rel="noopener noreferrer">{source.title}<span aria-hidden="true">↗</span></a>
        : supportLinkKind(source.url) === "local" ? <Link href={source.url!}>{source.title}<span aria-hidden="true">→</span></Link> : <span>{source.title}</span>}</li>)}</ul>
    </details>}
    {!!navigation.length && <nav className="support-next-actions" aria-label={t("chat.nextSteps")}>
      <small>{t("chat.nextSteps")}</small>
      {navigation.map(action => <Link
        key={action.id ?? action.url}
        href={action.url}
        data-chat-action={action.onActivate ? action.id : undefined}
        onClick={action.onActivate ? (event) => {
          event.preventDefault();
          action.onActivate?.();
        } : undefined}
      >{action.label}<span aria-hidden="true">→</span></Link>)}
    </nav>}
  </div>;
}
