import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ANTHROPIC_BASE_URL, canProbeProviderConnection, ProviderCreateDialog, ProviderProbeDialog, providerCredentialLabel, providerKindOptions, providerPurposeOptions, providerProbeErrorMessage, providerProbeModes, providerProbeVersionMatches, providerTestStatus } from "@/components/gateway/provider-scenes";
import type { ProviderConnection } from "@/lib/gateway-types";

describe("provider connection test controls", () => {
  it("keeps the confirmation action enabled when the configured default model will be used", () => {
    const connection: ProviderConnection = {
      id: "openai-1",
      name: "openai-primary",
      display_name: "OpenAI primary",
      kind: "openai",
      purpose: "target",
      status: "active",
      credential: { configured: true, status: "active", version: 3 },
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    };
    const html = renderToStaticMarkup(createElement(ProviderProbeDialog, {
      connection,
      pending: false,
      onClose: () => undefined,
      onRun: async () => undefined,
    }));

    expect(html).toContain("Leave blank to use the configured default");
    expect(html).toContain("Ping / Kiểm tra");
    expect(html).not.toContain("disabled=\"\"");
    expect(html).not.toContain("required=\"\"");
  });

  it("disables testing without an active credential", () => {
    expect(canProbeProviderConnection({ configured: false })).toBe(false);
    expect(canProbeProviderConnection({ configured: true, status: "revoked" })).toBe(false);
    expect(canProbeProviderConnection({ configured: true, status: "active" })).toBe(true);
  });

  it("maps known quota and timeout errors to useful safe messages", () => {
    expect(providerProbeErrorMessage("QUOTA_EXCEEDED")).toMatch(/quota/i);
    expect(providerProbeErrorMessage("TIMEOUT")).toMatch(/timed out/i);
    expect(providerProbeErrorMessage("API_KEY_REDACT_ME")).not.toContain("API_KEY_REDACT_ME");
  });

  it("tests the JEV binding with the JEV mode and preserves credential version matching", () => {
    expect(providerProbeModes("typesafe")).toEqual(["jev"]);
    expect(providerProbeVersionMatches(3, 3)).toBe(true);
    expect(providerProbeVersionMatches(2, 3)).toBe(false);
    expect(providerProbeVersionMatches(null, 0)).toBe(true);
    expect(providerProbeVersionMatches(null, 3)).toBe(false);
  });

  it("offers Anthropic as a target-only provider with a chat connection probe", () => {
    expect(providerKindOptions.map((item) => item.kind)).toContain("anthropic");
    expect(providerPurposeOptions("anthropic")).toEqual(["target"]);
    expect(providerProbeModes("anthropic")).toEqual(["chat"]);
    expect(canProbeProviderConnection({ configured: false }, "anthropic")).toBe(false);
    expect(canProbeProviderConnection({ configured: true, status: "active" }, "anthropic")).toBe(true);
  });

  it("locks the Anthropic connection form to target purpose and the official base URL", () => {
    const html = renderToStaticMarkup(createElement(ProviderCreateDialog, {
      name: "anthropic", displayName: "Anthropic", kind: "anthropic", purpose: "target",
      baseUrl: ANTHROPIC_BASE_URL, newKey: "", error: null, pending: false,
      onNameChange: () => undefined, onDisplayNameChange: () => undefined, onKindChange: () => undefined,
      onPurposeChange: () => undefined, onBaseUrlChange: () => undefined, onNewKeyChange: () => undefined,
      onClose: () => undefined, onSave: () => undefined,
    }));
    expect(html).toContain('value="https://api.anthropic.com"');
    expect(html).toContain('disabled="" placeholder=');
    expect(html).toContain('<option value="target" selected="">');
    expect(html).not.toContain('<option value="internal">');
    expect(html).toContain("Anthropic");
  });

  it("distinguishes revoked credentials from missing credentials without exposing the key", () => {
    expect(providerCredentialLabel({ configured: false, status: "revoked", version: 3, key_last4: "1234" }, "openai")).toContain("revoked");
    expect(providerCredentialLabel({ configured: false }, "openai")).toBe("Not configured");
    expect(providerCredentialLabel({ configured: false }, "offline")).toBe("Not required");
  });

  it("resets the displayed test state when the credential version changes", () => {
    expect(providerTestStatus({ configured: true, status: "active", version: 2, last_test_status: "healthy", last_test_credential_version: 1 })).toBe("untested");
    expect(providerTestStatus({ configured: true, status: "active", version: 2, last_test_status: "healthy", last_test_credential_version: 2 })).toBe("healthy");
  });
});
