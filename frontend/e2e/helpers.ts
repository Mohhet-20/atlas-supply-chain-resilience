import { expect, type Page } from "@playwright/test";

export const USER = process.env.ATLAS_E2E_USERNAME ?? "e2e-admin";
export const PASSWORD = process.env.ATLAS_E2E_PASSWORD ?? "Synthetic-E2E-Pass-Phrase-41!";

/** Sign in through the real login form. The token lives in sessionStorage, so each test signs in once. */
export async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText(/Signed in as/)).toBeVisible();
}

/** Collect uncaught page errors, console errors and Content-Security-Policy violations so a test can assert none
 * occurred. The app is served with the production CSP (frontend/csp.ts), so a violation is a real defect. */
export function trackErrors(page: Page): string[] {
  const errors: string[] = [];
  void page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) =>
      console.error(`csp-violation: ${e.violatedDirective} ${e.blockedURI}`),
    );
  });
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`console: ${m.text()}`);
  });
  return errors;
}

/** Every page of the app with the heading it must render. */
export const ROUTES: [string, RegExp][] = [
  ["/", /Overview/], ["/analytics", /Enterprise dashboard/], ["/alerting", /Alert & incident/],
  ["/suppliers", /Suppliers/], ["/facilities", /Facilities/], ["/products", /Products/], ["/network", /Network overview/],
  ["/network/explorer", /Network explorer/], ["/registry", /Registry/], ["/quality", /Data quality/], ["/data", /Data operations/],
  ["/risk", /Supplier risk/], ["/scenarios", /Scenario Studio/], ["/impact", /Cascading Impact/], ["/inventory", /Inventory & Stockout/],
  ["/forecast", /Demand Forecasting/], ["/leadtime", /Delivery Intelligence/], ["/models", /Model Evaluation/],
  ["/transport", /Transportation/], ["/sourcing", /Sourcing/], ["/replenishment", /Inventory Intelligence/],
  ["/resilience", /Resilience Optimization/], ["/decision", /Decision Center/], ["/integrations", /Integrations/], ["/admin", /Administration/],
];
