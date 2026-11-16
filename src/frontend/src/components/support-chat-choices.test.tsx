import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { SupportChatChoices } from "@/components/support-chat-choices";

describe("guided chatbot choices", () => {
  it("renders labelled button choices and escapes application names", () => {
    const html = renderToStaticMarkup(<SupportChatChoices choices={[
      { label: "Usage và Billing", message: "Usage khác Billing thế nào?" },
      { label: "<script>test23</script>", message: "Tôi chọn application test23" },
    ]} disabled={false} onSelect={() => {}} />);
    expect(html).toContain('role="group"');
    expect(html).toContain("Usage và Billing");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
  });
  it("disables historical choices so they cannot alter the current pending topic", () => {
    const html = renderToStaticMarkup(<SupportChatChoices choices={[
      { label: "7 ngày", message: "Thôi thì 7 ngày đi" },
    ]} disabled onSelect={() => {}} />);
    expect(html).toContain('disabled=""');
  });
});
