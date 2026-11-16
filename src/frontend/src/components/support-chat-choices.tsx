"use client";

import { useLanguage } from "@/components/language-provider";

export type SupportChoice = { label: string; message: string };

export function SupportChatChoices({ choices, disabled, onSelect }: {
  choices: SupportChoice[];
  disabled: boolean;
  onSelect: (message: string) => void;
}) {
  const { t } = useLanguage();
  if (!choices.length) return null;
  return <div className="beta-chat-choices" role="group" aria-label={t("chat.chooseTopic")}>
    {choices.map((choice, index) => <button
      key={`${choice.label}-${choice.message}`}
      type="button"
      disabled={disabled}
      onClick={() => onSelect(choice.message)}
    >
      <span className="beta-chat-choice-letter" aria-hidden="true">{String.fromCharCode(65 + index)}</span>
      <span>{choice.label}</span>
      <span className="beta-chat-choice-arrow" aria-hidden="true">→</span>
    </button>)}
  </div>;
}
