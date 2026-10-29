import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("uncertain external result stays separate from the observer count", async ({
  page,
}, info) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("./");
  await page
    .getByLabel("Scenario", { exact: true })
    .selectOption("after_effect_opaque");
  await page.getByRole("button", { name: "Show final state" }).click();
  await expect(page.locator(".metrics")).toContainText("unknown");
  await expect(page.locator(".metrics")).toContainText("Absent");
  await expect(page.locator(".observer strong")).toHaveText("1");
  await expect(page.locator("#mode")).toHaveText(/Recorded Python run/);
  await page.screenshot({
    path: info.outputPath("unknown.png"),
    fullPage: true,
  });
  await page
    .getByLabel("Scenario", { exact: true })
    .selectOption("after_effect_reconcile");
  await page.getByRole("button", { name: "Show final state" }).click();
  await expect(page.locator(".metrics")).toContainText("succeeded");
  await expect(page.locator(".metrics")).toContainText("Recorded");
  await expect(page.locator(".observer strong")).toHaveText("1");
  expect(errors).toEqual([]);
});
test("replays the negative control without executing a new effect", async ({
  page,
}) => {
  await page.goto("./");
  await page
    .getByLabel("Scenario", { exact: true })
    .selectOption("negative_control");
  await page.getByRole("button", { name: "Show final state" }).click();
  await expect(page.locator(".warning")).toContainText("intentionally");
  await expect(page.locator(".observer strong")).toHaveText("2");
  const requests: string[] = [];
  page.on("request", (r) => requests.push(r.url()));
  await page.getByRole("button", { name: "Replay captured run" }).click();
  await expect(page.locator(".observer strong")).toHaveText("0");
  expect(requests).toEqual([]);
});
test("every scenario is navigable with explicit recorded-run conditions", async ({
  page,
}) => {
  await page.goto("./");
  await expect(page.locator("#scenario")).toBeEnabled();
  const ids = await page
    .locator("#scenario option")
    .evaluateAll((nodes) => nodes.map((n) => (n as HTMLOptionElement).value));
  expect(ids).toHaveLength(13);
  for (const id of ids) {
    await page.locator("#scenario").selectOption(id);
    await page.getByRole("button", { name: "Show final state" }).click();
    await expect(page.locator(".assertions")).not.toContainText("FAIL");
  }
  await page.getByText("Run conditions and limits", { exact: true }).click();
  await expect(
    page.getByText(/Lease time uses a controlled logical clock/),
  ).toBeVisible();
});
test("keyboard focus and narrow layout remain usable", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const external: string[] = [];
  await page.route("**/*", (route) => {
    if (new URL(route.request().url()).origin !== "http://127.0.0.1:4175") {
      external.push(route.request().url());
      return route.abort();
    }
    return route.continue();
  });
  await page.goto("./");
  await expect(page.locator("#scenario")).toBeEnabled();
  const next = page.getByRole("button", { name: "Next step", exact: true });
  await next.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".event h3")).toBeFocused();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: info.outputPath("mobile.png"),
    fullPage: true,
  });
  expect(external).toEqual([]);
});
test("malformed records produce an error, not an invented successful run", async ({
  page,
}) => {
  await page.route("**/reports.json", (r) =>
    r.fulfill({ json: { version: 99, reports: [] } }),
  );
  await page.goto("./");
  await expect(page.getByRole("alert")).toHaveText("Unsupported report bundle");
  await expect(page.locator("#run")).toBeDisabled();
});
test("report text cannot add HTML", async ({ page, request }) => {
  const value = await (await request.get("/reports.json")).json();
  value.reports[0].title = '<img src=x onerror="window.injected=true">';
  await page.route("**/reports.json", (r) => r.fulfill({ json: value }));
  await page.goto("./");
  await expect(
    page.getByRole("heading", { name: value.reports[0].title }),
  ).toBeVisible();
  expect(await page.locator("#app img").count()).toBe(0);
  expect(await page.evaluate(() => Object.hasOwn(window, "injected"))).toBe(
    false,
  );
});
