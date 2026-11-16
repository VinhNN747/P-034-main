const docsOrigin = "https://minminhmin.github.io";
const docsBasePath = "/taskway.github.io";

export function taskwayDocsUrl(language: "vi" | "en", page = "authentication") {
  return `${docsOrigin}${docsBasePath}/${language}/docs/${page}/`;
}

export function supportLinkKind(value: string | null | undefined): "local" | "documentation" | null {
  if (!value || /[\\\u0000-\u0020]/.test(value)) return null;
  if (value.startsWith("/") && !value.startsWith("//")) return "local";
  try {
    const url = new URL(value);
    if (url.origin === docsOrigin && !url.username && !url.password
      && /^\/taskway\.github\.io\/(?:vi|en)\/docs\//.test(url.pathname)) return "documentation";
  } catch { /* An unsupported URL is rendered as plain text. */ }
  return null;
}
