import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SupportMessageContent } from "@/components/support-message-content";
import { SupportChatReferences } from "@/components/support-chat-references";
import { codeTokens, splitSupportMessage } from "@/lib/support-message-format";

describe("inert code examples", () => {
  it("keeps multiple code blocks, indentation and surrounding text", () => {
    const parts = splitSupportMessage('Intro\n```python\nif True:\n    print("hi")\n```\nThen\n```json\n{"ok": true}\n```\nDone');
    expect(parts.map(part => part.kind)).toEqual(["text", "code", "text", "code", "text"]);
    expect(parts[1]).toEqual({ kind: "code", language: "python", code: 'if True:\n    print("hi")' });
    expect(splitSupportMessage("```js\nalert(1)")[0]).toEqual({ kind: "code", language: "js", code: "alert(1)" });
    expect(codeTokens('const text = "<script>";', "js").map(token => token.text).join("")).toBe('const text = "<script>";');
  });
  it("renders code as escaped text, with copy, line numbers and read-only notice", () => {
    const html = renderToStaticMarkup(<SupportMessageContent content={'Use `client` and **API key**.\n```javascript\nconst html = "<script>alert(1)</script>";\n```'} />);
    expect(html).toContain("support-code-example");
    expect(html).toContain("support-code-keyword");
    expect(html).toContain("<ol>");
    expect(html).toContain("<code>client</code>");
    expect(html).toContain("<strong>API key</strong>");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("iframe");
    expect(html).toContain("Sao chép");
    expect(html).toContain("không thực thi");
  });
  it("collapses long examples without losing the copyable code", () => {
    const html = renderToStaticMarkup(<SupportMessageContent content={`\n\`\`\`python\n${Array.from({ length: 20 }, (_, i) => `print(${i})`).join("\n")}\n\`\`\``} />);
    expect(html.match(/<li>/g)).toHaveLength(14);
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain("(20)");
  });
});

describe("related sources and navigation", () => {
  it("groups sources in collapsed disclosure and separates direct route suggestions", () => {
    const url = "https://minminhmin.github.io/taskway.github.io/vi/docs/authentication/#api-keys";
    const html = renderToStaticMarkup(<SupportChatReferences sources={[
      { id: "docs", title: "Authentication", url },
      { id: "invalid", title: "<script>Title</script>", url: "javascript:alert(1)" },
    ]} actions={[
      { label: "Read authentication", url },
      { label: "Tạo application", url: "/applications/new" },
      { label: "Unsafe", url: "https://evil.example" },
    ]} />);
    expect(html).toContain('<details class="support-related-links">');
    expect(html).not.toContain(" open=");
    expect(html).toContain("Các đường dẫn liên quan");
    expect(html.match(/Authentication/g)).toHaveLength(1);
    expect(html).toContain('target="_blank" rel="noopener noreferrer"');
    expect(html).toContain('href="/applications/new"');
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain("evil.example");
    expect(html).not.toContain("<script>");
  });
});
