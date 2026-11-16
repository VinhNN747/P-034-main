"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { useLanguage } from "@/components/language-provider";
import { taskwayDocsUrl } from "@/lib/taskway-docs";

type Language = "python" | "node";

const quickstart = {
  python: {
    label: "Python",
    install: "pip install openai",
    code: `import os
import uuid
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["TASKWAY_API_KEY"],
    base_url=os.environ["TASKWAY_BASE_URL"],  # e.g. https://taskway-ai-be.onrender.com/v1
)

# Reuse this key if you retry the same request.
idempotency_key = str(uuid.uuid4())
response = client.chat.completions.create(
    model="auto",  # Follow this application's routing policy.
    messages=[
        {"role": "user", "content": "Explain what an API gateway does in one sentence."}
    ],
    extra_headers={"Idempotency-Key": idempotency_key},
)

print(response.choices[0].message.content)`,
  },
  node: {
    label: "Node.js",
    install: "npm install openai",
    code: `import { randomUUID } from "node:crypto";
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.TASKWAY_API_KEY,
  baseURL: process.env.TASKWAY_BASE_URL, // e.g. https://taskway-ai-be.onrender.com/v1
});

// Reuse this key if you retry the same request.
const idempotencyKey = randomUUID();
const response = await client.chat.completions.create(
  {
    model: "auto", // Follow this application's routing policy.
    messages: [
      { role: "user", content: "Explain what an API gateway does in one sentence." },
    ],
  },
  {
    headers: { "Idempotency-Key": idempotencyKey },
  },
);

console.log(response.choices[0]?.message.content);`,
  },
} satisfies Record<Language, { label: string; install: string; code: string }>;

const examples = [
  {
    title: "Responses API",
    description: "Use the Responses endpoint with the same client configuration.",
    python: `import uuid

idempotency_key = str(uuid.uuid4())
response = client.responses.create(
    model="auto",
    input="Summarize the purpose of an AI gateway in one sentence.",
    extra_headers={"Idempotency-Key": idempotency_key},
)

print(response.output_text)`,
    node: `import { randomUUID } from "node:crypto";

const idempotencyKey = randomUUID();
const response = await client.responses.create(
  {
    model: "auto",
    input: "Summarize the purpose of an AI gateway in one sentence.",
  },
  { headers: { "Idempotency-Key": idempotencyKey } },
);

console.log(response.output_text);`,
  },
  {
    title: "Embeddings",
    description: "Replace the placeholder with an embedding model available to your application.",
    python: `import uuid

idempotency_key = str(uuid.uuid4())
result = client.embeddings.create(
    model="<available-embedding-model>",
    input="AI Gateway quickstart",
    extra_headers={"Idempotency-Key": idempotency_key},
)

print(result.data[0].embedding[:5])`,
    node: `import { randomUUID } from "node:crypto";

const idempotencyKey = randomUUID();
const result = await client.embeddings.create(
  {
    model: "<available-embedding-model>",
    input: "AI Gateway quickstart",
  },
  { headers: { "Idempotency-Key": idempotencyKey } },
);

console.log(result.data[0].embedding.slice(0, 5));`,
  },
];

type CodeTokenKind = "comment" | "string" | "number" | "keyword" | "literal" | "constant" | "function" | "property";

const keywords: Record<Language, Set<string>> = {
  python: new Set(["import", "from", "as", "def", "class", "return", "if", "elif", "else", "for", "in", "async", "await", "with", "try", "except", "and", "or", "not", "pass"]),
  node: new Set(["import", "from", "as", "const", "let", "var", "new", "return", "async", "await", "if", "else", "try", "catch", "throw", "export", "default", "function", "class", "extends"]),
};

const literals: Record<Language, Set<string>> = {
  python: new Set(["True", "False", "None"]),
  node: new Set(["true", "false", "null", "undefined", "NaN", "Infinity"]),
};

const codeTokenPattern = /\/\/[^\r\n]*|#[^\r\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b\d+(?:\.\d+)?\b|\b[A-Za-z_$][\w$]*\b/g;

function tokenKind(code: string, token: string, start: number, language: Language): CodeTokenKind | null {
  if (token.startsWith("//") || token.startsWith("#")) return "comment";
  if (token.startsWith("\"") || token.startsWith("'")) return "string";
  if (/^\d/.test(token)) return "number";
  if (keywords[language].has(token)) return "keyword";
  if (literals[language].has(token)) return "literal";
  if (/^[A-Z][A-Z0-9_]*$/.test(token)) return "constant";

  const end = start + token.length;
  if (/^\s*\(/.test(code.slice(end))) return "function";
  if (language === "node" && /^\s*:/.test(code.slice(end))) return "property";
  if (/(?:\?\.|\.)\s*$/.test(code.slice(0, start))) return "property";
  return null;
}

function highlightCode(code: string, language: Language): ReactNode[] {
  const parts: ReactNode[] = [];
  let cursor = 0;

  for (const match of code.matchAll(codeTokenPattern)) {
    const token = match[0];
    const start = match.index ?? cursor;
    if (start > cursor) parts.push(code.slice(cursor, start));

    const kind = tokenKind(code, token, start, language);
    parts.push(kind ? <span className={`sdk-token sdk-token--${kind}`} key={`${start}-${token.length}`}>{token}</span> : token);
    cursor = start + token.length;
  }

  if (cursor < code.length) parts.push(code.slice(cursor));
  return parts;
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const { t } = useLanguage();
  const [status, setStatus] = useState<"idle" | "copied" | "error">("idle");

  useEffect(() => {
    if (status === "idle") return;
    const timeout = window.setTimeout(() => setStatus("idle"), 1800);
    return () => window.clearTimeout(timeout);
  }, [status]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setStatus("copied");
    } catch {
      setStatus("error");
    }
  }

  return (
    <button type="button" className="sdk-copy-button" onClick={() => void copy()} aria-label={label} aria-live="polite">
      {status === "copied" ? "Copied" : status === "error" ? t("sdk.copyFailed") : t("sdk.copy")}
    </button>
  );
}

export function SdkGuideScene() {
  const { t, language: interfaceLanguage } = useLanguage();
  const [language, setLanguage] = useState<Language>("python");
  const activeQuickstart = quickstart[language];

  return (
    <div className="sdk-guide-page">
      <section className="gateway-page-heading">
        <div>
          <p className="gateway-eyebrow">{t("sdk.developer")}</p>
          <h1>{t("sdk.title")}</h1>
          <p>{t("sdk.description")}</p>
        </div>
        <Link href="/applications" className="gateway-button gateway-button--secondary">{t("sdk.viewApplications")}</Link>
      </section>

      <section className="sdk-guide-steps" aria-label={t("sdk.quickSteps")}>
        <article><span>01</span><div><h2>{t("sdk.createApplication")}</h2><p>{t("sdk.chooseApplication")}</p></div></article>
        <article><span>02</span><div><h2>{t("sdk.createApiKey")}</h2><p>{t("sdk.copyKey")}</p></div></article>
        <article><span>03</span><div><h2>{t("sdk.sendRequest")}</h2><p>{t("sdk.callGateway")}</p></div></article>
      </section>

      <div className="sdk-guide-layout">
        <div className="sdk-guide-main">
          <section className="gateway-panel sdk-guide-quickstart" id="quickstart">
            <div className="gateway-section-heading">
              <div><p className="gateway-eyebrow">{t("sdk.quickstart")}</p><h2>{t("sdk.firstRequest")}</h2></div>
            </div>
            <div className="sdk-guide-key-note" role="note">
              <strong>{t("sdk.useKey")}</strong>
              <span>{t("sdk.keyNote")}</span>
            </div>
            <div className="sdk-guide-idempotency-note" role="note">{t("sdk.idempotency")}</div>
            <p className="sdk-guide-env-hint">{t("sdk.envHint")}</p>
            <div className="sdk-code-card">
              <div className="sdk-code-toolbar">
                <div className="sdk-language-tabs" role="tablist" aria-label={t("sdk.language")}>
                  {(Object.keys(quickstart) as Language[]).map((key) => (
                    <button
                      key={key}
                      id={`sdk-tab-${key}`}
                      type="button"
                      role="tab"
                      aria-selected={language === key}
                      aria-controls="sdk-code-panel"
                      className={language === key ? "is-active" : ""}
                      onClick={() => setLanguage(key)}
                    >
                      {quickstart[key].label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="sdk-install-line">
                <span>{t("sdk.install")}</span>
                <code>{activeQuickstart.install}</code>
                <CopyButton value={activeQuickstart.install} label={`Copy ${activeQuickstart.label} install command`} />
              </div>
              <div className="sdk-code-toolbar sdk-code-toolbar--snippet">
                <span>{t("sdk.firstRequest")}</span>
                <CopyButton value={activeQuickstart.code} label={`Copy ${activeQuickstart.label} quickstart code`} />
              </div>
              <div role="tabpanel" id="sdk-code-panel" aria-labelledby={`sdk-tab-${language}`}>
                <pre><code>{highlightCode(activeQuickstart.code, language)}</code></pre>
              </div>
            </div>
          </section>

          <section className="gateway-panel sdk-guide-gateway-note" id="gateway-settings">
            <div className="sdk-guide-gateway-note-heading">
              <div><p className="gateway-eyebrow">{t("sdk.gatewaySettings")}</p><h2>{t("sdk.syntax")}</h2></div>
              <a className="sdk-guide-docs-link" href={taskwayDocsUrl(interfaceLanguage)} target="_blank" rel="noopener noreferrer">
                {t("sdk.productDocs")} <span aria-hidden="true">↗</span>
              </a>
            </div>
            <p>{t("sdk.gatewayNote")}</p>
          </section>

          <section className="sdk-guide-examples" id="more-examples" aria-labelledby="sdk-examples-title">
            <div className="gateway-section-heading">
              <div><p className="gateway-eyebrow">{t("sdk.moreExamples")}</p><h2 id="sdk-examples-title">{t("sdk.otherApis")}</h2></div>
            </div>
            <div className="sdk-example-grid">
              {examples.map((example) => (
                <article className="gateway-panel sdk-example-card" key={example.title}>
                  <div className="sdk-example-heading"><div><h3>{example.title}</h3><p>{example.description}</p></div><CopyButton value={example[language]} label={`Copy ${example.title} ${activeQuickstart.label} example`} /></div>
                  <pre><code>{highlightCode(example[language], language)}</code></pre>
                </article>
              ))}
            </div>
          </section>

          <section className="gateway-panel sdk-guide-help" id="troubleshooting">
            <div><p className="gateway-eyebrow">{t("sdk.troubleshooting")}</p><h2>{t("sdk.commonErrors")}</h2></div>
            <ul>
              <li><strong>401:</strong> {t("sdk.error401")}</li>
              <li><strong>{t("sdk.missingIdempotency")}:</strong> {t("sdk.missingIdempotencyHint")}</li>
              <li><strong>{t("sdk.modelNotFound")}:</strong> {t("sdk.modelNotFoundHint")}</li>
              <li><strong>{t("sdk.unsupportedParameter")}:</strong> {t("sdk.unsupportedParameterHint")}</li>
            </ul>
          </section>
        </div>

        <aside className="gateway-panel sdk-guide-toc" aria-label={t("sdk.onPage")}>
          <p className="gateway-eyebrow">{t("sdk.onPage")}</p>
          <a href="#quickstart">{t("sdk.quickstart")}</a>
          <a href="#gateway-settings">{t("sdk.gatewaySettings")}</a>
          <a href="#more-examples">{t("sdk.moreExamples")}</a>
          <a href="#troubleshooting">{t("sdk.troubleshooting")}</a>
        </aside>
      </div>
    </div>
  );
}
