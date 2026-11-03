import { existsSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";

// Browser end-to-end tests against the real API (scripts/e2e_server.py: synthetic demo database, real sign-in)
// and a production build of this app served by `vite preview`. Nothing is mocked.
const API_PORT = 8765;
const WEB_PORT = 4180;
const WEB = `http://127.0.0.1:${WEB_PORT}`;
const venv = ["../backend/.venv/Scripts/python.exe", "../backend/.venv/bin/python"].find((p) => existsSync(p));
const python = process.env.ATLAS_PYTHON ?? venv ?? "python";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // one shared database; tests that change state run in a fixed order
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: {
    baseURL: WEB,
    // System Chrome, so no browser download is needed; set PW_CHANNEL=chromium to use Playwright's own build.
    channel: process.env.PW_CHANNEL ?? "chrome",
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
  },
  projects: [{ name: "chrome", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      command: `"${python}" ../scripts/e2e_server.py --port ${API_PORT} --frontend-origin ${WEB}`,
      url: `http://127.0.0.1:${API_PORT}/api/v1/health`,
      timeout: 180_000,
      reuseExistingServer: false,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command: `npm run build && npx vite preview --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      url: WEB,
      timeout: 180_000,
      reuseExistingServer: false,
      env: { VITE_API_BASE_URL: `http://127.0.0.1:${API_PORT}/api/v1` },
    },
  ],
});
