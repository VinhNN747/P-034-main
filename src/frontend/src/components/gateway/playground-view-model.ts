import type { GatewayRequestDetail } from "@/lib/gateway-types";

type PlaygroundCopyKey = "playground.taskLabel" | "playground.subtaskOutput" | "playground.noTaskOutput" | "playground.fallbackUsedFor" | "playground.fallbackUsed" | "playground.noFallback";
type PlaygroundCopy = (key: PlaygroundCopyKey, params?: Record<string, string | number>) => string;

const englishCopy: Record<PlaygroundCopyKey, string> = {
  "playground.taskLabel": "Task {index}",
  "playground.subtaskOutput": "Subtask output",
  "playground.noTaskOutput": "No output was returned for this task.",
  "playground.fallbackUsedFor": "Fallback used for {label}",
  "playground.fallbackUsed": "Fallback used",
  "playground.noFallback": "No fallback",
};

function defaultCopy(key: PlaygroundCopyKey, params?: Record<string, string | number>): string {
  return Object.entries(params ?? {}).reduce((value, [name, replacement]) => value.replaceAll(`{${name}}`, String(replacement)), englishCopy[key]);
}

export type PlaygroundTraceViewModel = {
  stage: string;
  strategy: string;
  status: string;
  latency: string;
  error?: string;
};

export type PlaygroundTaskViewModel = {
  taskId: string;
  label: string;
  description: string;
  status: string;
  fallback: boolean;
  output: string;
  model: string;
  provider: string;
  latency: string;
  cost: string;
  tokens: string;
  error?: string;
};

export type PlaygroundViewModel = {
  requestId: string;
  status: string;
  output: string;
  provider: string;
  model: string;
  latency: string;
  cost: string;
  tokens: string;
  fallback: string;
  traces: PlaygroundTraceViewModel[];
  tasks: PlaygroundTaskViewModel[];
};

function text(value: unknown, fallback = "—"): string {
  return typeof value === "string" && value ? value : fallback;
}

function providerLabel(value: string): string {
  const normalized = value.toLowerCase();
  if (normalized === "google" || normalized === "gemini") return "Google Gemini";
  if (normalized === "openai") return "OpenAI";
  if (normalized === "offline" || normalized === "local") return "Offline";
  return value;
}

function taskOrdinal(taskId: string, fallback: number): number {
  const match = taskId.match(/(?:^|[-_])(\d+)$/);
  return match ? Number(match[1]) : fallback;
}

export function toPlaygroundViewModel(request: GatewayRequestDetail, copy: PlaygroundCopy = defaultCopy): PlaygroundViewModel {
  const executionByTask = new Map(
    request.executions.map((execution) => [String(execution.task_id ?? ""), execution]),
  );
  const orderedTasks = [...request.tasks].sort((left, right) =>
    taskOrdinal(String(left.task_id ?? ""), 0) - taskOrdinal(String(right.task_id ?? ""), 0),
  );
  const tasks = orderedTasks.map((task, index) => {
    const taskId = String(task.task_id ?? `task-${index + 1}`);
    const execution = executionByTask.get(taskId);
    const ordinal = taskOrdinal(taskId, index + 1);
    const fallbackUsed = execution?.fallback === true;
    return {
      taskId,
      label: copy("playground.taskLabel", { index: ordinal }),
      description: text(task.description, text(task.expected_output, copy("playground.subtaskOutput"))),
      status: fallbackUsed ? "success · fallback" : text(execution?.status, "pending"),
      fallback: fallbackUsed,
      output: text(execution?.result_payload, copy("playground.noTaskOutput")),
      model: text(execution?.model_name, text(execution?.model)),
      provider: providerLabel(text(execution?.provider_name, text(execution?.provider))),
      latency: `${Math.round(Number(execution?.latency_ms ?? 0))}ms`,
      cost: Number.isFinite(Number(execution?.cost_usd))
        ? `USD $${Number(execution?.cost_usd ?? 0).toFixed(6)}`
        : "—",
      tokens: `${Number(execution?.input_tokens ?? 0) + Number(execution?.output_tokens ?? 0)} tokens`,
      error: typeof execution?.failure_reason === "string" ? execution.failure_reason : undefined,
    };
  });
  const taskOutput = tasks.length > 1
    ? tasks.map((task) => `## ${task.label}\n\n${task.output}`).join("\n\n")
    : text(request.output, "The gateway returned no final answer.");
  const successfulExecutions = request.executions.filter((execution) => execution.status === "success");
  const usedModels = [...new Set(successfulExecutions.map((execution) =>
    text(execution.model_name, text(execution.model, "")),
  ).filter(Boolean))];
  const usedProviders = [...new Set(successfulExecutions.map((execution) =>
    providerLabel(text(execution.provider_name, text(execution.provider, ""))),
  ).filter(Boolean))];
  const fallbackTask = tasks.find((task) => task.fallback);
  const fallbackReason = text(request.fallback_reason, "");

  return {
    requestId: request.request_id,
    status: request.status,
    output: taskOutput,
    provider: usedProviders.join(" + ") || providerLabel(text(request.provider)),
    model: usedModels.join(" + ") || text(request.model),
    latency: `${Math.round(request.latency_ms ?? 0)}ms`,
    cost: `USD $${(request.estimated_cost ?? 0).toFixed(6)}`,
    tokens: `${request.total_tokens ?? 0} tokens`,
    fallback: request.fallback_used
      ? `${fallbackTask ? copy("playground.fallbackUsedFor", { label: fallbackTask.label }) : copy("playground.fallbackUsed")}${fallbackReason ? `: ${fallbackReason}` : ""}`
      : copy("playground.noFallback"),
    tasks,
    traces: request.traces.map((trace) => ({
      stage: text(trace.stage),
      strategy: text(trace.strategy),
      status: text(trace.status),
      latency: `${Math.round(Number(trace.latency_ms ?? 0))}ms`,
      error: typeof trace.error === "string" ? trace.error : undefined,
    })),
  };
}
