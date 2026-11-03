import { expect, test } from "@playwright/test";
import { PASSWORD, ROUTES, signIn, trackErrors, USER } from "./helpers";

const API = "http://127.0.0.1:8765/api/v1";

// Release-gate browser tests against the real API and the synthetic demo world (scripts/e2e_server.py).


test.describe("authentication", () => {
  test("rejects a wrong password with a generic message and keeps no session", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Username").fill(USER);
    await page.getByLabel("Password").fill("not-the-password-123");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toContainText(/invalid|incorrect/i);
    expect(await page.evaluate(() => window.sessionStorage.getItem("atlas.registry.token"))).toBeNull();
  });

  test("data pages refuse to load without a session", async ({ page }) => {
    await page.goto("/suppliers");
    await expect(page.getByRole("alert").first()).toContainText(/token|sign in|session/i);
    await expect(page.getByText("DEMO Meridian Metals")).toHaveCount(0);
  });

  test("signs in, survives a reload, and signs out", async ({ page }) => {
    await signIn(page);
    await page.goto("/suppliers"); // full reload: the tab-scoped session must persist and be sent by every client
    await expect(page.getByText("DEMO Meridian Metals")).toBeVisible();
    await page.getByRole("link", { name: "Account" }).click();
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect.poll(() => page.evaluate(() => window.sessionStorage.getItem("atlas.registry.token"))).toBeNull();
    await page.goto("/suppliers");
    await expect(page.getByText("DEMO Meridian Metals")).toHaveCount(0);
  });

  test("the API refuses an invalid bearer token", async ({ request }) => {
    const r = await request.get(`${API}/suppliers`, { headers: { Authorization: "Bearer atl_s_forged" } });
    expect(r.status()).toBe(401);
    const ok = await request.post(`${API}/auth/login`, { data: { username: USER, password: PASSWORD } });
    expect(ok.status()).toBe(200);
  });
});

test.describe("navigation", () => {
  test("the app is served with the production Content-Security-Policy", async ({ request }) => {
    const r = await request.get("/");
    const csp = r.headers()["content-security-policy"] ?? "";
    expect(csp).toContain("script-src 'self'");
    expect(csp).toContain("style-src 'self'");
    expect(csp).not.toContain("unsafe-inline");
    expect(r.headers()["x-frame-options"]).toBe("DENY");
  });

  for (const [path, heading] of ROUTES) {
    test(`${path} loads without errors`, async ({ page }) => {
      const errors = trackErrors(page);
      await signIn(page);
      await page.goto(path);
      await expect(page.locator("h1").first()).toHaveText(heading);
      await page.waitForTimeout(1500); // let the page's API calls settle
      await expect(page.getByText("Something went wrong")).toHaveCount(0);
      expect(errors).toEqual([]);
    });
  }
});

test.describe("demo decision workflow", () => {
  test.beforeEach(async ({ page }) => signIn(page));

  test("network: demo suppliers and facilities are on the overview", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("DEMO Meridian Metals").first()).toBeVisible();
    await expect(page.getByText("DEMO Plant Alpha").first()).toBeVisible();
  });

  test("risk: the high-risk supplier is identified with its evidence", async ({ page }) => {
    await page.goto("/risk/suppliers");
    const row = page.getByRole("row", { name: /DEMO Meridian Metals/ });
    await expect(row).toContainText(/high/i);
    await row.getByRole("link").first().click();
    await expect(page.getByRole("heading", { name: /DEMO Meridian Metals/ }).first()).toBeVisible();
    await expect(page.getByText(/evidence/i).first()).toBeVisible();
  });

  test("scenario and cascading impact: the supplier outage run is recorded", async ({ page }) => {
    await page.goto("/scenarios");
    await expect(page.getByText("DEMO Core supplier outage").first()).toBeVisible();
    await page.goto("/impact");
    await expect(page.getByText(/DEMO Core supplier outage|completed/i).first()).toBeVisible();
  });

  test("inventory: the projected stockout is labelled as a projection", async ({ page }) => {
    await page.goto("/inventory");
    await expect(page.getByText(/projected stockout/i).first()).toBeVisible();
  });

  test("alerts: both demo alerts are listed and one can be acknowledged", async ({ page }) => {
    await page.goto("/alerting");
    const risk = page.getByText("Supplier risk 56 at DEMO Meridian Metals");
    await expect(risk).toBeVisible();
    await expect(page.getByText(/Projected stockout: DEMO Controller Unit/)).toBeVisible();
    await risk.click(); // opens the alert detail
    await page.getByRole("button", { name: /^Acknowledge/ }).click();
    await expect(page.getByText(/acknowledged/i).first()).toBeVisible();
  });

  test("audit: the acknowledgement is in the audit log and the hash chain verifies", async ({ request }) => {
    const login = await request.post(`${API}/auth/login`, { data: { username: USER, password: PASSWORD } });
    const headers = { Authorization: `Bearer ${(await login.json()).token}` };
    const ack = await request.get(`${API}/registry/audit`, { headers, params: { action: "alerting.alert.acknowledge" } });
    expect(ack.status()).toBe(200);
    expect((await ack.json()).items.length).toBeGreaterThan(0);
    const verify = await request.get(`${API}/registry/audit/verify`, { headers });
    expect(verify.status()).toBe(200);
    expect(await verify.json()).toMatchObject({ ok: true, first_bad_event_id: null });
  });
});
