"use client";

import { useEffect, useRef, useState, type MouseEvent } from "react";
import { useLanguage } from "@/components/language-provider";
import { codeTokens, splitSupportMessage } from "@/lib/support-message-format";

function CodeExample({ code, language, demoCopy = false }: { code: string; language: string; demoCopy?: boolean }) {
  const { t } = useLanguage();
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");
  const [expanded, setExpanded] = useState(false);
  const demoResetTimer = useRef<number | null>(null);
  const lines = code.split("\n");
  const visible = expanded ? lines : lines.slice(0, 14);

  useEffect(() => () => {
    if (demoResetTimer.current !== null) window.clearTimeout(demoResetTimer.current);
  }, []);

  function showDemoCopied() {
    if (demoResetTimer.current !== null) window.clearTimeout(demoResetTimer.current);
    setCopyState("copied");
    demoResetTimer.current = window.setTimeout(() => {
      setCopyState("idle");
      demoResetTimer.current = null;
    }, 1400);
  }

  async function copy(event: MouseEvent<HTMLButtonElement>) {
    if (demoCopy && !event.nativeEvent.isTrusted) {
      showDemoCopied();
      return;
    }
    if (demoResetTimer.current !== null) {
      window.clearTimeout(demoResetTimer.current);
      demoResetTimer.current = null;
    }
    try {
      await navigator.clipboard.writeText(code);
      setCopyState("copied");
    } catch { setCopyState("error"); }
  }
  return <section className="support-code-example" aria-label={`${t("chat.codeExample")} ${language}`}>
    <header><span><i aria-hidden="true">&lt;/&gt;</i> {language}</span><button type="button" data-faq-demo-copy={demoCopy ? "" : undefined} onClick={(event) => void copy(event)}>{copyState === "copied" ? t("chat.codeCopied") : t("chat.codeCopy")}</button></header>
    <div className="support-code-scroll" tabIndex={0} aria-label={t("chat.codeExample")}>
      <ol>{visible.map((line, index) => <li key={index}><code>{line ? codeTokens(line, language).map((token, i) => <span key={i} className={token.kind ? `support-code-${token.kind}` : undefined}>{token.text}</span>) : " "}</code></li>)}</ol>
    </div>
    {lines.length > 14 && <button type="button" className="support-code-expand" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? t("chat.codeCollapse") : `${t("chat.codeExpand")} (${lines.length})`}</button>}
    <footer>{t("chat.codeReadOnly")}<span role="status">{copyState === "error" ? t("chat.codeCopyError") : copyState === "copied" ? t("chat.codeCopied") : ""}</span></footer>
  </section>;
}

export function SupportMessageContent({ content, demoCopy = false }: { content: string; demoCopy?: boolean }) {
  return <div className="support-message-content">{splitSupportMessage(content).map((part, index) => part.kind === "code"
    ? <CodeExample key={index} code={part.code} language={part.language} demoCopy={demoCopy} />
    : <div key={index} className="support-message-text">{part.text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g).map((text, i) => text.startsWith("`") && text.endsWith("`")
      ? <code key={i}>{text.slice(1, -1)}</code> : text.startsWith("**") && text.endsWith("**") ? <strong key={i}>{text.slice(2, -2)}</strong> : text)}</div>)}</div>;
}
