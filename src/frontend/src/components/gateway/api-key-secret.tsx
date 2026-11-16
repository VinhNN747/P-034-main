"use client";

import { useState } from "react";
import { useLanguage } from "@/components/language-provider";

export function OneTimeApiKeySecret({ rawKey }: { rawKey: string }) {
  const { t } = useLanguage();
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(rawKey);
      setCopied(true);
      setCopyFailed(false);
    } catch {
      setCopied(false);
      setCopyFailed(true);
    }
  }

  return <div className="notice notice--success api-key-secret" role="alert"><strong>{t("keys.copyNow")}</strong><p>{t("keys.copyDescription")}</p><code dir="ltr" translate="no">{rawKey}</code><button type="button" className="gateway-button gateway-button--secondary" onClick={() => void copy()}>{copied ? t("keys.copied") : t("keys.copy")}</button>{copyFailed && <p role="status">{t("keys.copyFailed")}</p>}</div>;
}
