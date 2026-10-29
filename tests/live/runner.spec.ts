import { expect, test } from "@playwright/test";

for (const [scenario, execution, receipt] of [
  ["after_effect_opaque", "unknown", "Absent"],
  ["after_effect_reconcile", "succeeded", "Recorded"],
  ["after_dispatch", "succeeded", "Recorded"],
] as const) {
  test(`executes ${scenario} through the real local Python API`, async ({
    page,
  }) => {
    await page.goto("./?live=1");
    await page.getByLabel("Scenario", { exact: true }).selectOption(scenario);
    const result = page.waitForResponse(
      (r) =>
        r.url().endsWith(`/api/scenarios/${scenario}`) &&
        r.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Run scenario with local Python" })
      .click();
    const response = await result;
    expect(response.status()).toBe(200);
    const report = await response.json();
    expect(report.crash_exit_codes).toEqual([73]);
    expect(Object.values(report.checks).every(Boolean)).toBe(true);
    await expect(page.locator("#mode")).toHaveText(
      "Executed by the local Python server",
    );
    await page.getByRole("button", { name: "Show final state" }).click();
    await expect(page.locator(".metrics")).toContainText(execution);
    await expect(page.locator(".metrics")).toContainText(receipt);
    await expect(page.locator(".observer strong")).toHaveText("1");
  });
}
test("the local runner rejects cross-origin requests and arbitrary parameters", async ({
  request,
}) => {
  const denied = await request.post("/api/scenarios/happy", {
    headers: {
      Origin: "https://example.invalid",
      "Content-Type": "application/json",
    },
    data: {},
  });
  expect(denied.status()).toBe(403);
  const invalid = await request.post("/api/scenarios/happy", {
    headers: {
      Origin: "http://127.0.0.1:4176",
      "Content-Type": "application/json",
    },
    data: { command: "arbitrary" },
  });
  expect(invalid.status()).toBe(400);
});

test("a failed local execution does not display a stale successful result", async ({
  page,
}) => {
  await page.goto("./?live=1");
  await expect(page.locator("#scenario")).toBeEnabled();
  await page.route("**/api/scenarios/*", (route) =>
    route.fulfill({ status: 500, json: { error: "fixture failure" } }),
  );
  await page
    .getByRole("button", { name: "Run scenario with local Python" })
    .click();
  await expect(page.getByRole("alert")).toContainText("did not complete");
  await expect(page.locator("#mode")).toContainText("no fresh result");
  expect(await page.locator(".metrics").count()).toBe(0);
  expect(await page.locator(".assertions").count()).toBe(0);
});
