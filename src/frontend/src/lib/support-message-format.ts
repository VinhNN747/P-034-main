export type SupportMessagePart = { kind: "text"; text: string } | { kind: "code"; code: string; language: string };

/** A small, inert formatter: no HTML, Markdown links, eval or code execution. */
export function splitSupportMessage(content: string): SupportMessagePart[] {
  const parts: SupportMessagePart[] = [];
  const fence = /(?:^|\n)[ \t]*```([^\n`]*)\n([\s\S]*?)(?:\n[ \t]*```[ \t]*(?=\n|$)|$)/g;
  let offset = 0;
  for (const match of content.matchAll(fence)) {
    const start = match.index!;
    if (start > offset) parts.push({ kind: "text", text: content.slice(offset, start) });
    const label = match[1].trim().toLowerCase();
    parts.push({ kind: "code", code: match[2].replace(/\n$/, ""), language: /^[a-z0-9+#.-]{1,20}$/.test(label) ? label : "text" });
    offset = start + match[0].length;
  }
  if (offset < content.length) parts.push({ kind: "text", text: content.slice(offset) });
  return parts;
}

export function codeTokens(line: string, language: string): { text: string; kind?: string }[] {
  if (!["python", "py", "javascript", "js", "typescript", "ts", "json", "bash", "sh", "shell", "curl"].includes(language)) return [{ text: line }];
  const pattern = /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\/\/.*$|#.*$|\b(?:import|from|as|def|class|return|if|else|elif|for|in|await|async|const|let|var|new|function|throw|try|except|catch|export|True|False|None|true|false|null)\b|\b\d+(?:\.\d+)?\b)/g;
  const tokens: { text: string; kind?: string }[] = [];
  let offset = 0;
  for (const match of line.matchAll(pattern)) {
    if (match.index! > offset) tokens.push({ text: line.slice(offset, match.index) });
    const text = match[0];
    const kind = /^["']/.test(text) ? "string" : /^(?:#|\/\/)/.test(text) ? "comment" : /^\d/.test(text) ? "number" : "keyword";
    tokens.push({ text, kind });
    offset = match.index! + text.length;
  }
  if (offset < line.length) tokens.push({ text: line.slice(offset) });
  return tokens;
}
