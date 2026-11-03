import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { ROUTES, signIn } from "./helpers";

// Automated WCAG 2.1 A/AA checks (axe-core) on every page and the sign-in form, against the real API and demo data.
// Automated rules find roughly a third to a half of accessibility problems; this is not a substitute for a manual audit.

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

function summary(violations: { id: string; impact?: string | null; nodes: { target: unknown }[] }[]) {
  return violations.map((v) => `${v.impact ?? "?"} ${v.id}: ${v.nodes.map((n) => JSON.stringify(n.target)).join(", ")}`);
}

test("sign-in page has no WCAG 2.1 AA violations", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByLabel("Username")).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  expect(summary(results.violations)).toEqual([]);
});

for (const [path, heading] of ROUTES) {
  test(`${path} has no WCAG 2.1 AA violations`, async ({ page }) => {
    await signIn(page);
    await page.goto(path);
    await expect(page.locator("h1").first()).toHaveText(heading);
    await page.waitForTimeout(1500); // let data load so the scan covers the rendered content
    const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
    expect(summary(results.violations)).toEqual([]);
  });
}
