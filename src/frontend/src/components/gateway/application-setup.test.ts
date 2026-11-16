import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/gateway-api", () => ({
  createApplication: vi.fn(),
  createApplicationKey: vi.fn(),
}));

import { createApplication, createApplicationKey } from "@/lib/gateway-api";
import { completeApplicationSetup } from "@/components/gateway/application-setup";
import type { ApplicationCreateInput, ApplicationDetail, ApplicationKeyCreated } from "@/lib/gateway-types";

const input: ApplicationCreateInput = { name: "Support", slug: "support", environment: "development" };
const application: ApplicationDetail = { name: input.name, slug: input.slug, id: "app-new", owner_user_id: "user-1", environment: "development", status: "active" };
const key: ApplicationKeyCreated = { id: "key-new", name: "support-development", raw_key: "ak_test_once", key_prefix: "ak_test", created_at: "2026-10-15T00:00:00Z", revoked: false };

describe("application setup with an optional API key", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(createApplication).mockResolvedValue(application);
    vi.mocked(createApplicationKey).mockResolvedValue(key);
  });

  it("creates the key for the newly saved application and returns its one-time secret", async () => {
    const result = await completeApplicationSetup(input, "support-development");

    expect(createApplication).toHaveBeenCalledWith(input);
    expect(createApplicationKey).toHaveBeenCalledWith("app-new", { name: "support-development" });
    expect(result).toEqual({ application, key, keyError: null });
  });

  it("skips the key request when the user opts out", async () => {
    await expect(completeApplicationSetup(input, null)).resolves.toEqual({ application, key: null, keyError: null });
    expect(createApplicationKey).not.toHaveBeenCalled();
  });

  it("uses the user's key name without deriving one from the application", async () => {
    await completeApplicationSetup(input, "  My production SDK  ");

    expect(createApplicationKey).toHaveBeenCalledWith("app-new", { name: "My production SDK" });
  });

  it.each(["", "   ", "k".repeat(101)])("rejects an invalid key name before creating an application (%s)", async (keyName) => {
    await expect(completeApplicationSetup(input, keyName)).rejects.toThrow("API key name must contain 1 to 100 characters.");
    expect(createApplication).not.toHaveBeenCalled();
    expect(createApplicationKey).not.toHaveBeenCalled();
  });

  it("retains the saved application after a key error so retry does not create a duplicate application", async () => {
    const failure = new Error("Key creation unavailable");
    vi.mocked(createApplicationKey).mockRejectedValueOnce(failure);

    const first = await completeApplicationSetup(input, "support-development");
    expect(first).toEqual({ application, key: null, keyError: failure });

    const retried = await completeApplicationSetup(input, "support-development", first.application);
    expect(createApplication).toHaveBeenCalledTimes(1);
    expect(createApplicationKey).toHaveBeenCalledTimes(2);
    expect(retried.key?.raw_key).toBe("ak_test_once");
  });

  it("does not issue a key if application creation fails", async () => {
    vi.mocked(createApplication).mockRejectedValueOnce(new Error("Application already exists"));

    await expect(completeApplicationSetup(input, "support-development")).rejects.toThrow("Application already exists");
    expect(createApplicationKey).not.toHaveBeenCalled();
  });
});
