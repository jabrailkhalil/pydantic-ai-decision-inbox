import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { parseBundle, parseReport } from "../../src/report";
const raw = readFileSync(
  new URL("../../public/reports.json", import.meta.url),
  "utf8",
);
const fixture = () => JSON.parse(raw);

describe("captured Python reports", () => {
  it("loads all thirteen actual scenario records", () => {
    const reports = parseBundle(raw);
    expect(reports).toHaveLength(13);
    expect(reports.every((r) => Object.values(r.checks).every(Boolean))).toBe(
      true,
    );
    expect(
      reports.find((r) => r.id === "negative_control")!.steps.at(-1)!
        .observed_effects,
    ).toBe(2);
    const unknown = reports
      .find((r) => r.id === "after_effect_opaque")!
      .steps.at(-1)!;
    expect(unknown.execution).toBe("unknown");
    expect(unknown.has_receipt).toBe(false);
    expect(unknown.observed_effects).toBe(1);
    expect(
      reports.find((r) => r.id === "after_effect_reconcile")!.steps.at(-1)!
        .execution,
    ).toBe("succeeded");
  });
  it.each([null, false, [], "text", 42])(
    "rejects non-object report %j",
    (value) => {
      expect(() => parseReport(value)).toThrow();
    },
  );
  it.each([
    (r: any) => {
      r.id = "../command";
    },
    (r: any) => {
      r.title = "";
    },
    (r: any) => {
      r.title = "x".repeat(2001);
    },
    (r: any) => {
      r.mode = ["fixture-idempotent"];
    },
    (r: any) => {
      r.steps = [];
    },
    (r: any) => {
      r.steps = Array(51).fill(r.steps[0]);
    },
    (r: any) => {
      r.steps[0].decision = ["approved"];
    },
    (r: any) => {
      r.steps[0].execution = "done";
    },
    (r: any) => {
      r.steps[0].observed_effects = -1;
    },
    (r: any) => {
      r.steps[0].observed_effects = 0.5;
    },
    (r: any) => {
      r.steps[0].observed_effects = "1";
    },
    (r: any) => {
      r.steps[0].has_receipt = "false";
    },
    (r: any) => {
      r.steps[0].execution = "succeeded";
    },
    (r: any) => {
      r.steps[0].agent_finished = true;
    },
    (r: any) => {
      r.checks = {};
    },
    (r: any) => {
      r.checks.test = "true";
    },
    (r: any) => {
      r.crash_exit_codes = [256];
    },
    (r: any) => {
      r.assumptions = false;
    },
  ])("rejects invalid report case %#", (mutate) => {
    const value = fixture().reports[0];
    mutate(value);
    expect(() => parseReport(value)).toThrow();
  });
  it("preserves failing assertions rather than turning them into success", () => {
    const value = fixture().reports[0];
    value.checks.actual_failed_control = false;
    expect(parseReport(value).checks.actual_failed_control).toBe(false);
  });
  it.each(["{", "null", "[]", '{"version":2}', '{"version":1,"reports":[]}'])(
    "rejects invalid bundles %s",
    (value) => expect(() => parseBundle(value)).toThrow(),
  );
  it("rejects duplicated scenario identities", () => {
    const value = fixture();
    value.reports.push(value.reports[0]);
    expect(() => parseBundle(JSON.stringify(value))).toThrow("Duplicate");
  });
  it("bounds the bundle size before parsing", () => {
    expect(() => parseBundle("x".repeat(1024 * 1024 + 1))).toThrow("1 MiB");
  });
});
