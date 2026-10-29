import { defineConfig, devices } from "@playwright/test";
const python = process.env.DECISION_INBOX_PYTHON ?? "python";
export default defineConfig({
  outputDir: "test-results-live",
  testDir: "tests/live",
  retries: 0,
  workers: 1,
  use: { baseURL: "http://127.0.0.1:4176", trace: "retain-on-failure" },
  projects: [{ name: "chromium-live", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `"${python}" -m pydantic_ai_decision_inbox serve --directory dist --port 4176`,
    url: "http://127.0.0.1:4176",
    reuseExistingServer: !process.env.CI,
  },
});
