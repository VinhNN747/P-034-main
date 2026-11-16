import { createApplication, createApplicationKey } from "@/lib/gateway-api";
import type { ApplicationCreateInput, ApplicationDetail, ApplicationKeyCreated } from "@/lib/gateway-types";

type ApplicationSetupResult = {
  application: ApplicationDetail;
  key: ApplicationKeyCreated | null;
  keyError: unknown;
};

// Keep the saved application available if the separate key request fails.
export async function completeApplicationSetup(
  input: ApplicationCreateInput,
  keyName: string | null,
  savedApplication: ApplicationDetail | null = null,
): Promise<ApplicationSetupResult> {
  const normalizedKeyName = keyName?.trim() ?? null;
  if (normalizedKeyName !== null && (!normalizedKeyName || normalizedKeyName.length > 100)) {
    throw new Error("API key name must contain 1 to 100 characters.");
  }
  const application = savedApplication ?? await createApplication(input);
  if (normalizedKeyName === null) return { application, key: null, keyError: null };

  try {
    const key = await createApplicationKey(application.id, { name: normalizedKeyName });
    return { application, key, keyError: null };
  } catch (keyError) {
    return { application, key: null, keyError };
  }
}
