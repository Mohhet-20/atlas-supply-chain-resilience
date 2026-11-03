import { test } from "@playwright/test";
import { signIn } from "./helpers";

// Captures genuine screenshots of the running app over the synthetic demo world (not part of the release gate):
//   SCREENSHOTS=1 npx playwright test e2e/screenshots.spec.ts
// Images are written to docs/screenshots/ at the configured 1440x900 viewport.
const SHOTS: [string, string][] = [
  ["01-overview", "/"],
  ["02-executive-dashboard", "/analytics"],
  ["03-network", "/network"],
  ["04-supplier-risk", "/risk/suppliers"],
  ["05-supplier-risk-evidence", "/risk/suppliers/1"],
  ["06-scenario-run", "/scenarios/runs/1"],
  ["07-cascading-impact", "/impact/1"],
  ["08-inventory-stockout", "/inventory/analyses/1"],
  ["09-alerts", "/alerting"],
];

for (const [name, path] of SHOTS) {
  test(`screenshot ${name}`, async ({ page }) => {
    test.skip(!process.env.SCREENSHOTS, "set SCREENSHOTS=1 to capture");
    await signIn(page);
    await page.goto(path);
    await page.locator("h1").first().waitFor();
    await page.waitForTimeout(2500);
    await page.screenshot({ path: `../docs/screenshots/${name}.png` });
  });
}
