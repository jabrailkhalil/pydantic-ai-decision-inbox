import "./style.css";
import { parseBundle, parseReport, type Report } from "./report";

const app = document.querySelector<HTMLDivElement>("#app")!;
const select = document.querySelector<HTMLSelectElement>("#scenario")!;
const run = document.querySelector<HTMLButtonElement>("#run")!;
const status = document.querySelector<HTMLParagraphElement>("#status")!;
const error = document.querySelector<HTMLParagraphElement>("#error")!;
const mode = document.querySelector<HTMLParagraphElement>("#mode")!;
const live = new URL(location.href).searchParams.get("live") === "1";
let reports: Report[] = [],
  current: Report,
  position = 0;
let liveResult = false,
  busy = false;
let failed = false;
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}
function choose(): void {
  current = reports.find((r) => r.id === select.value)!;
  position = 0;
  liveResult = false;
  failed = false;
  error.textContent = "";
  render(false);
}
function render(focus: boolean): void {
  app.setAttribute("aria-busy", String(busy));
  app.replaceChildren();
  mode.textContent = liveResult
    ? "Executed by the local Python server"
    : "Recorded Python run · replay changes only the view";
  select.disabled = busy;
  run.disabled = busy;
  run.textContent = live
    ? "Run scenario with local Python"
    : "Replay captured run";
  if (failed) {
    mode.textContent = "The local run failed; no fresh result is shown";
    app.append(
      el(
        "p",
        "Select a scenario or retry the local runner. The previous result is not displayed as a new outcome.",
      ),
    );
    return;
  }
  if (busy) {
    app.append(
      el("p", "The local lab is running. No production action is connected."),
    );
    return;
  }
  const step = current.steps[position]!;
  const card = el("section");
  card.className = "panel";
  const heading = el("h2", current.title);
  heading.tabIndex = -1;
  card.append(
    heading,
    el("p", `PydanticAI ${current.sdk_version} · ${current.mode}`),
  );
  if (current.id === "negative_control") {
    const warning = el(
      "p",
      "Negative control: this scenario intentionally demonstrates repeated execution.",
    );
    warning.className = "warning";
    card.append(warning);
  }
  const metrics = el("dl");
  metrics.className = "metrics";
  for (const [label, value] of [
    ["Inbox decision", step.decision],
    ["Dispatch state", step.execution],
    ["Inbox receipt", step.has_receipt ? "Recorded" : "Absent"],
    ["Agent continuation", step.agent_finished ? "Finished" : "Pending"],
  ]) {
    const item = el("div");
    item.append(el("dt", label), el("dd", value));
    if (value === "unknown") item.className = "uncertain";
    metrics.append(item);
  }
  const observed = el("div");
  observed.className = "observer";
  observed.append(
    el("span", "Effects observed by the test harness"),
    el("strong", String(step.observed_effects)),
    el("p", "This observer count is separate from an inbox execution receipt."),
  );
  const event = el("section");
  event.className = "event";
  const title = el("h3", step.title);
  title.tabIndex = -1;
  event.append(
    el("p", `Step ${position + 1} of ${current.steps.length}`),
    title,
  );
  if (step.note) event.append(el("p", step.note));
  const controls = el("div");
  controls.className = "controls";
  const previous = el("button", "Previous step");
  previous.disabled = position === 0;
  const next = el("button", "Next step");
  next.disabled = position === current.steps.length - 1;
  const last = el("button", "Show final state");
  last.disabled = next.disabled;
  previous.onclick = () => {
    position--;
    render(true);
  };
  next.onclick = () => {
    position++;
    render(true);
  };
  last.onclick = () => {
    position = current.steps.length - 1;
    render(true);
  };
  controls.append(previous, next, last);
  card.append(metrics, observed, event, controls);
  const assertions = el("section");
  assertions.className = "assertions";
  const list = el("ul");
  for (const [key, passed] of Object.entries(current.checks))
    list.append(
      el("li", `${passed ? "PASS" : "FAIL"} · ${key.replaceAll("_", " ")}`),
    );
  assertions.append(
    el("h2", "Assertions for the complete run"),
    el("p", "These checks cover the entire trace, including later steps."),
    list,
  );
  const conditions = el("details");
  const assumptions = el("ul");
  for (const assumption of current.assumptions)
    assumptions.append(el("li", assumption));
  conditions.append(
    el("summary", "Run conditions and limits"),
    assumptions,
    el(
      "p",
      `Injected child exit codes: ${current.crash_exit_codes.length ? current.crash_exit_codes.join(", ") : "none"}`,
    ),
  );
  app.append(card, assertions, conditions);
  if (focus) app.querySelector("h3")!.focus();
}
select.onchange = choose;
run.onclick = async () => {
  error.textContent = "";
  if (!live) {
    position = 0;
    render(true);
    return;
  }
  busy = true;
  failed = false;
  status.textContent = "Running the local Python scenario…";
  render(false);
  try {
    const response = await fetch(
      `./api/scenarios/${encodeURIComponent(current.id)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      },
    );
    if (!response.ok)
      throw new Error(
        "The local runner did not complete. Start the Python lab server to execute scenarios.",
      );
    const report = parseReport(await response.json());
    if (report.id !== current.id)
      throw new Error("The runner returned a different scenario");
    current = report;
    position = 0;
    liveResult = true;
    status.textContent =
      "Local execution completed. Inspect the steps and assertions below.";
  } catch (e) {
    failed = true;
    error.textContent = e instanceof Error ? e.message : "Scenario failed";
    status.textContent = "";
  } finally {
    busy = false;
    render(true);
  }
};
void fetch("./reports.json")
  .then(async (response) => {
    if (!response.ok) throw new Error("Recorded reports could not be loaded");
    reports = parseBundle(await response.text());
    for (const report of reports) {
      const option = el("option", report.title);
      option.value = report.id;
      select.append(option);
    }
    choose();
  })
  .catch((e: unknown) => {
    app.setAttribute("aria-busy", "false");
    app.replaceChildren();
    error.textContent =
      e instanceof Error ? e.message : "Unable to load reports";
  });
