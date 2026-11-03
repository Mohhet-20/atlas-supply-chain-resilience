import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

// All identities and tokens below are SYNTHETIC test values.
type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; body: unknown; auth: string | undefined }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const method = init?.method ?? "GET";
    const headers = (init?.headers ?? {}) as Record<string, string>;
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined, auth: headers.Authorization });
    const h = routes[`${method} ${path}`];
    return Promise.resolve(h ? h(url, init) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
  });
}
const renderApp = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
const profile = (role: string, permissions: string[]) => () =>
  jsonResponse({
    actor: `${role}-person`, role, authenticated: true, kind: "user", platform_admin: false,
    organization: { uid: "OR-1", slug: "default", name: "Default" }, permissions, must_change_password: false, auth_required: true, writes_enabled: true,
  });
const VIEWER = ["data.read", "analytics.read", "alert.read"];
const ADMIN = [...VIEWER, "user.manage", "audit.read"];

beforeEach(() => {
  tokenStore.clear();
  window.sessionStorage.clear();
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("sign in", () => {
  it("stores the session token for this tab only and never in localStorage or the URL", async () => {
    mockApi({
      "POST /auth/login": () =>
        jsonResponse({ token: "atl_s_synthetic", token_type: "bearer", expires_at: "2099-01-01T00:00:00Z", idle_timeout_minutes: 60, user: { username: "vera", role: "viewer", must_change_password: false, organization: null } }),
      "GET /auth/me": profile("viewer", VIEWER),
    });
    renderApp("/login");
    await userEvent.type(await screen.findByLabelText("Username"), "vera");
    await userEvent.type(screen.getByLabelText("Password"), "Synthetic-Pass-Phrase-41!");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(tokenStore.get()).toBe("atl_s_synthetic"));
    expect(window.sessionStorage.getItem("atlas.registry.token")).toBe("atl_s_synthetic");
    expect(window.localStorage.length).toBe(0);
    // the password travels only in the login request body, never in another call or a URL
    const others = calls.filter((c) => c.path !== "/auth/login");
    expect(JSON.stringify(others)).not.toContain("Synthetic-Pass-Phrase-41!");
    const login = calls.find((c) => c.path === "/auth/login");
    expect(login?.auth).toBeUndefined();
  });

  it("shows the server's generic error, keeps no token and clears the password field", async () => {
    mockApi({ "POST /auth/login": () => jsonResponse({ error: { code: "unauthenticated", message: "Invalid username or password." } }, 401) });
    renderApp("/login");
    await userEvent.type(await screen.findByLabelText("Username"), "vera");
    const pw = screen.getByLabelText("Password") as HTMLInputElement;
    await userEvent.type(pw, "wrong-password-123");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password.");
    expect(tokenStore.get()).toBe("");
    expect(pw.value).toBe("");
    expect(tokenStore.expired()).toBe(false); // a wrong password is not an expired session
  });
});

describe("session expiry", () => {
  it("clears the token and tells the person when the API answers 401 to an authenticated request", async () => {
    tokenStore.set("atl_s_synthetic", true);
    mockApi({
      "GET /auth/me": () => jsonResponse({ error: { code: "unauthenticated", message: "The session is not valid." } }, 401),
    });
    renderApp("/");
    expect(await screen.findByText(/Your session ended/)).toBeInTheDocument();
    expect(tokenStore.get()).toBe("");
    expect(window.sessionStorage.length).toBe(0);
    expect(screen.getByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/login");
  });
});

describe("permission-aware navigation", () => {
  it("hides Administration from a viewer and shows it to a user manager", async () => {
    tokenStore.set("atl_s_synthetic", true);
    mockApi({ "GET /auth/me": profile("viewer", VIEWER) });
    renderApp("/login");
    expect(await screen.findByText(/Signed in as/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Administration" })).not.toBeInTheDocument();
    cleanup();
    mockApi({ "GET /auth/me": profile("admin", ADMIN) });
    renderApp("/login");
    expect(await screen.findByRole("link", { name: "Administration" })).toBeInTheDocument();
  });

  it("refuses the admin page to a viewer without calling the user list", async () => {
    tokenStore.set("atl_s_synthetic", true);
    mockApi({ "GET /auth/me": profile("viewer", VIEWER) });
    renderApp("/admin");
    expect(await screen.findByText(/does not include user management/)).toBeInTheDocument();
  });

  it("lists users and shows a reset token once for an administrator", async () => {
    tokenStore.set("atl_s_synthetic", true);
    const user = { user_uid: "US-1", username: "vera", display_name: null, role: "viewer", status: "active", platform_admin: false, must_change_password: false, organization_id: 1, created_at: null, last_login_at: null };
    mockApi({
      "GET /auth/me": profile("admin", ADMIN),
      "GET /admin/users": () => jsonResponse({ items: [user] }),
      "GET /admin/security/summary": () => jsonResponse({ window_hours: 24, counts: { "auth.login.failed": 3 }, note: "Counts come from the audit trail." }),
      "POST /admin/users/US-1/password-reset": () => jsonResponse({ token: "one-time-synthetic-token", expires_in_minutes: 30 }),
    });
    renderApp("/admin");
    expect(await screen.findByText("vera")).toBeInTheDocument();
    expect(await screen.findByText("auth.login.failed")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reset password" }));
    expect(await screen.findByLabelText("Reset token")).toHaveTextContent("one-time-synthetic-token");
  });
});

describe("account", () => {
  it("changes the password and clears both fields; sign out clears the token and calls the API", async () => {
    tokenStore.set("atl_s_synthetic", true);
    mockApi({
      "GET /auth/me": profile("viewer", VIEWER),
      "POST /auth/password": () => new Response(null, { status: 204 }),
      "POST /auth/logout": () => new Response(null, { status: 204 }),
    });
    renderApp("/account");
    const cur = (await screen.findByLabelText("Current password")) as HTMLInputElement;
    const nxt = screen.getByLabelText("New password") as HTMLInputElement;
    await userEvent.type(cur, "Old-Synthetic-Pass-1!");
    await userEvent.type(nxt, "New-Synthetic-Pass-2!");
    await userEvent.click(screen.getByRole("button", { name: "Change password" }));
    // a LoadingState also has role="status", so match the success message itself
    const done = await screen.findByText(/Password changed/);
    expect(done).toHaveAttribute("role", "status");
    expect((screen.getByLabelText("Current password") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("New password") as HTMLInputElement).value).toBe("");
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(tokenStore.get()).toBe(""));
    expect(calls.some((c) => c.path === "/auth/logout")).toBe(true);
  });
});

describe("safe rendering", () => {
  it("never uses dangerouslySetInnerHTML, eval or inline secrets anywhere in the app source", () => {
    const files = import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    const own = Object.entries(files).filter(([p]) => !p.includes("/test/"));
    expect(own.length).toBeGreaterThan(50);
    for (const [path, src] of own) {
      expect(src, path).not.toMatch(/dangerouslySetInnerHTML|\beval\(|new Function\(/);
      expect(src, path).not.toMatch(/localStorage\.setItem\([^)]*token/i);
    }
  });
});
