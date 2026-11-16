"use client";

import { useLanguage } from "@/components/language-provider";

export function ChatWelcome() {
  const { t } = useLanguage();

  return (
    <div>
      <p className="eyebrow">{t("chat.workspace")}</p>
      <h1>{t("chat.welcomeBack")}</h1>
      <p>{t("chat.sessionActive")}</p>
    </div>
  );
}
