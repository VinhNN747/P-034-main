import { describe, expect, it } from "vitest";

import { OneTimeApiKeySecret } from "@/components/gateway/api-key-secret";

describe("one-time API key secret", () => {
  it("has a dedicated copy-once component", () => {
    expect(OneTimeApiKeySecret).toBeTypeOf("function");
  });
});
