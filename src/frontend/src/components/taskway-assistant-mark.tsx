type Props = {
  className?: string;
};

export function TaskwayAssistantMark({ className = "" }: Props) {
  return (
    <svg
      className={`taskway-assistant-mark ${className}`.trim()}
      viewBox="0 0 36 36"
      role="img"
      aria-label="Taskway Assistant"
    >
      <rect className="taskway-assistant-mark__top" x="3" y="5" width="30" height="7.5" rx="3.75" />
      <rect className="taskway-assistant-mark__middle" x="8" y="14.25" width="21" height="7.5" rx="3.75" />
      <rect className="taskway-assistant-mark__bottom" x="11" y="23.5" width="18" height="7.5" rx="3.75" />
    </svg>
  );
}
