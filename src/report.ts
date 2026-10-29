export type Step = {
  title: string;
  decision: "pending" | "approved" | "denied";
  execution:
    "none" | "queued" | "claimed" | "dispatched" | "unknown" | "succeeded";
  has_receipt: boolean;
  agent_finished: boolean;
  observed_effects: number;
  note: string;
};
export type Report = {
  id: string;
  title: string;
  mode: "fixture-idempotent" | "fixture-opaque";
  sdk_version: string;
  steps: Step[];
  checks: Record<string, boolean>;
  crash_exit_codes: number[];
  assumptions: string[];
};
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Expected a report object");
  return value as Record<string, unknown>;
}
function text(value: unknown, empty = false): string {
  if (
    typeof value !== "string" ||
    (!empty && !value.trim()) ||
    value.length > 2000
  )
    throw new Error("Invalid report text");
  return value;
}
function bool(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("Invalid report boolean");
  return value;
}
export function parseReport(value: unknown): Report {
  const r = object(value);
  const id = text(r.id);
  if (!/^[a-z_]{1,60}$/.test(id)) throw new Error("Invalid scenario ID");
  if (
    typeof r.mode !== "string" ||
    !["fixture-idempotent", "fixture-opaque"].includes(r.mode)
  )
    throw new Error("Unknown executor mode");
  if (!Array.isArray(r.steps) || r.steps.length < 1 || r.steps.length > 50)
    throw new Error("Invalid step count");
  const steps: Step[] = r.steps.map((raw) => {
    const s = object(raw);
    if (
      typeof s.decision !== "string" ||
      !["pending", "approved", "denied"].includes(s.decision)
    )
      throw new Error("Unknown decision state");
    if (
      typeof s.execution !== "string" ||
      ![
        "none",
        "queued",
        "claimed",
        "dispatched",
        "unknown",
        "succeeded",
      ].includes(s.execution)
    )
      throw new Error("Unknown execution state");
    if (
      !Number.isSafeInteger(s.observed_effects) ||
      (s.observed_effects as number) < 0 ||
      (s.observed_effects as number) > 100
    )
      throw new Error("Invalid effect count");
    const receipt = bool(s.has_receipt);
    if (s.execution === "succeeded" && !receipt)
      throw new Error("Success requires a recorded receipt");
    if (receipt && s.execution !== "succeeded")
      throw new Error("Receipt requires a succeeded dispatch");
    if (s.decision !== "approved" && s.execution !== "none")
      throw new Error("Execution requires approval");
    if (
      s.agent_finished &&
      s.decision !== "denied" &&
      s.execution !== "succeeded"
    )
      throw new Error("Agent result requires denial or a receipt");
    return {
      title: text(s.title),
      decision: s.decision as Step["decision"],
      execution: s.execution as Step["execution"],
      has_receipt: receipt,
      agent_finished: bool(s.agent_finished),
      observed_effects: s.observed_effects as number,
      note: text(s.note, true),
    };
  });
  const rawChecks = object(r.checks);
  if (!Object.keys(rawChecks).length || Object.keys(rawChecks).length > 30)
    throw new Error("Invalid assertion count");
  const checks = Object.fromEntries(
    Object.entries(rawChecks).map(([key, value]) => [text(key), bool(value)]),
  );
  if (
    !Array.isArray(r.crash_exit_codes) ||
    r.crash_exit_codes.length > 10 ||
    r.crash_exit_codes.some((v) => !Number.isSafeInteger(v) || v < 0 || v > 255)
  )
    throw new Error("Invalid child exit codes");
  if (!Array.isArray(r.assumptions) || r.assumptions.length > 15)
    throw new Error("Invalid assumptions");
  return {
    id,
    title: text(r.title),
    mode: r.mode as Report["mode"],
    sdk_version: text(r.sdk_version),
    steps,
    checks,
    crash_exit_codes: r.crash_exit_codes as number[],
    assumptions: r.assumptions.map((s) => text(s)),
  };
}
export function parseBundle(raw: string): Report[] {
  if (new TextEncoder().encode(raw).byteLength > 1024 * 1024)
    throw new Error("Report bundle exceeds 1 MiB");
  const root = object(JSON.parse(raw));
  if (
    root.version !== 1 ||
    !Array.isArray(root.reports) ||
    root.reports.length < 1 ||
    root.reports.length > 30
  )
    throw new Error("Unsupported report bundle");
  const reports = root.reports.map(parseReport);
  if (new Set(reports.map((r) => r.id)).size !== reports.length)
    throw new Error("Duplicate scenario ID");
  return reports;
}
