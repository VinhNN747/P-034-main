import { describe, expect, it } from "vitest";
import { supportLinkKind, taskwayDocsUrl } from "./taskway-docs";

describe("reviewed support links", () => {
  it("allows Taskway documentation with section anchors and local product routes", () => {
    expect(supportLinkKind(taskwayDocsUrl("vi"))).toBe("documentation");
    expect(supportLinkKind(`${taskwayDocsUrl("en", "chat-completions")}#output-and-usage`)).toBe("documentation");
    expect(supportLinkKind("/applications/new")).toBe("local");
  });

  it.each([
    "https://evil.example/docs/authentication/",
    "https://minminhmin.github.io.evil.example/taskway.github.io/vi/docs/authentication/",
    "https://minminhmin.github.io/other-project/vi/docs/authentication/",
    "https://evil@minminhmin.github.io/taskway.github.io/vi/docs/authentication/",
    "http://minminhmin.github.io/taskway.github.io/vi/docs/authentication/",
    "//evil.example",
    "/\\evil.example",
    "javascript:alert(1)",
  ])("does not make an unreviewed external link clickable (%s)", (url) => {
    expect(supportLinkKind(url)).toBeNull();
  });
});
