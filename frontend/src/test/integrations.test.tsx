import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

// All identities, keys and secrets below are SYNTHETIC test values.
type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; body: unknown }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const method = init?.method ?? "GET";
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const h = routes[`${method} ${path}`];
    return Promise.resolve(h ? h(url, init) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
  });
}
const renderApp = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
const profile = (role: string, permissions: string[]) => () =>
  jsonResponse({ actor: `${role}-person`, role, authenticated: true, kind: "user", platform_admin: false, organization: { uid: "OR-1", slug: "default", name: "Default" }, permissions, must_change_password: false, auth_required: true, writes_enabled: true });
const VIEWER = ["data.read", "analytics.read", "alert.read"];
const REVIEWER = [...VIEWER, "integration.read"];
const ADMIN = [...REVIEWER, "integration.admin", "user.manage"];

const health = { state: "healthy", reasons: [], recent_runs: 2, error_rate: 0, partial_runs: 0, avg_duration_ms: 20, retries: 0, data_quality_failures: 0, last_success_at: null, last_failure_at: null, last_run_status: "succeeded" };
const integ = { integration_uid: "IG-AAA111", name: "Sandbox feed", category: "inventory", provider: "sandbox_inventory", description: null, config: {}, credential: { reference: "env:ATLAS_CRED_DEMO", status: "available" }, status: "healthy", enabled: true, schedule_minutes: 60, next_run_at: null, last_success_at: "2026-10-18T10:00:00Z", last_failure_at: null, error_summary: null, version: 1, health };
const run = { run_uid: "IR-RUN001", mode: "full", trigger: "manual", status: "partial", started_at: "2026-10-18T10:00:00Z", finished_at: null, duration_ms: 12, discovered: 30, processed: 28, created: 28, updated: 0, skipped: 0, rejected: 2, failed: 0, retries: 0, error_summary: "2 rejected", checkpoint: {} };
const connectors = {
  items: [
    { key: "sandbox_inventory", name: "Sandbox inventory feed", category: "inventory", provider: "ATLAS sandbox", description: "SANDBOX ONLY synthetic records.", status: "sandbox", version: "1.0", capabilities: [], needs_credential: false, config_fields: {}, creatable: true },
    { key: "sap", name: "SAP ERP", category: "erp", provider: "SAP", description: "EXTENSION POINT: not implemented.", status: "extension_point", version: "-", capabilities: [], needs_credential: false, config_fields: {}, creatable: false },
  ],
};
const overview = { total: 1, by_health: { healthy: 1 }, active_syncs: 0, webhook_queue_depth: 3, webhook_dead_letters: 1, sync_errors_recorded: 2, recent_failures: [{ run_uid: "IR-BAD001", error_summary: "auth: provider rejected the credentials", finished_at: null }] };
const page = <T,>(items: T[]) => jsonResponse({ items, total: items.length, limit: 50, offset: 0 });

function signIn(role: string, perms: string[], extra: Record<string, Handler> = {}) {
  tokenStore.set("atl_s_synthetic", true);
  return mockApi({
    "GET /auth/me": profile(role, perms),
    "GET /integrations/overview": () => jsonResponse(overview),
    "GET /integrations/connectors": () => jsonResponse(connectors),
    "GET /integrations": () => page([integ]),
    ...extra,
  });
}

beforeEach(() => {
  tokenStore.clear();
  window.sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("integration overview and catalogue", () => {
  it("shows counts, recent failures and accurate connector labels", async () => {
    signIn("reviewer", REVIEWER);
    renderApp("/integrations");
    expect(await screen.findByText("Sandbox feed")).toBeInTheDocument();
    expect(screen.getByText(/1 dead-lettered/)).toBeInTheDocument();
    expect(screen.getByRole("alert", { name: "" })).toHaveTextContent("provider rejected the credentials");
    const catalogue = screen.getByRole("region", { name: "Connector catalogue" });
    expect(within(catalogue).getByText("SANDBOX (synthetic data)")).toBeInTheDocument();
    expect(within(catalogue).getByText(/Extension point - not implemented/)).toBeInTheDocument();
  });

  it("offers only creatable connectors and lets an administrator create a draft with a credential reference, never a secret", async () => {
    signIn("admin", ADMIN, { "POST /integrations": () => jsonResponse({ ...integ, name: "New feed", status: "draft" }, 201) });
    renderApp("/integrations");
    const form = await screen.findByRole("form", { name: "Create integration" });
    expect(within(form).queryByRole("option", { name: "SAP ERP" })).toBeNull();
    await userEvent.type(within(form).getByLabelText("Name"), "New feed");
    await userEvent.type(within(form).getByLabelText(/Credential reference/), "env:ATLAS_CRED_DEMO");
    await userEvent.click(within(form).getByRole("button", { name: "Create draft" }));
    expect(await screen.findByText(/Created New feed as a draft/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST" && c.path === "/integrations")?.body).toEqual({ name: "New feed", provider: "sandbox_inventory", credential_ref: "env:ATLAS_CRED_DEMO" });
  });

  it("is read-only for a reviewer: no create form and a notice", async () => {
    signIn("reviewer", REVIEWER);
    renderApp("/integrations");
    expect(await screen.findByText("Sandbox feed")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Create integration" })).toBeNull();
    expect(screen.getByText(/Read-only/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "API keys" })).toBeNull();
  });
});

describe("permission restrictions", () => {
  it("refuses a viewer without calling any integration endpoint, and hides the nav link", async () => {
    signIn("viewer", VIEWER);
    renderApp("/integrations");
    expect(await screen.findByText(/does not include access to integrations/)).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/integrations"))).toBe(false);
    expect(screen.queryByRole("link", { name: "Integrations" })).toBeNull();
  });

  it("asks an anonymous visitor to sign in", async () => {
    mockApi({});
    renderApp("/integrations");
    expect(await screen.findByText(/needs a signed-in user account/)).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/integrations"))).toBe(false);
  });

  it("keeps the API keys page for administrators", async () => {
    signIn("reviewer", REVIEWER);
    renderApp("/integrations/keys");
    expect(await screen.findByText(/needs the administrator role/)).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/service-accounts"))).toBe(false);
  });
});

describe("integration detail and sync history", () => {
  const detailRoutes = (extra: Record<string, Handler> = {}) => ({
    "GET /integrations/IG-AAA111": () => jsonResponse(integ),
    "GET /integrations/IG-AAA111/runs": () => page([run]),
    "GET /integrations/runs/IR-RUN001": () => jsonResponse({ ...run, errors: [{ record_ref: "SBX-00003", code: "mapping", message: "cannot convert 'quantity' to float", at: null }] }),
    ...extra,
  });

  it("shows health, history and record-level errors", async () => {
    signIn("reviewer", REVIEWER, detailRoutes());
    renderApp("/integrations/IG-AAA111");
    expect(await screen.findByRole("heading", { name: "Sandbox feed" })).toBeInTheDocument();
    expect(screen.getByLabelText("Health")).toHaveTextContent("Healthy");
    expect(screen.getByText("env:ATLAS_CRED_DEMO (available)")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Show errors for IR-RUN001" }));
    expect(await screen.findByText(/cannot convert 'quantity' to float/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sync now" })).toBeNull(); // reviewers cannot start syncs
  });

  it("lets an administrator start a sync and shows the server's answer; a conflict is reported, not hidden", async () => {
    let n = 0;
    signIn("admin", ADMIN, detailRoutes({
      "POST /integrations/IG-AAA111/sync": () => (++n === 1 ? jsonResponse({ ...run, status: "queued" }, 202) : jsonResponse({ error: { code: "conflict", message: "A synchronisation is already queued or running for this integration." } }, 409)),
    }));
    renderApp("/integrations/IG-AAA111");
    await userEvent.click(await screen.findByRole("button", { name: "Sync now" }));
    expect(await screen.findByText("Incremental sync started.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sync now" }));
    expect(await screen.findByText(/already queued or running/)).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "POST" && c.path.endsWith("/sync")).map((c) => c.body)).toEqual([{ mode: "incremental" }, { mode: "incremental" }]);
  });

  it("shows a load error with a retry instead of an empty page", async () => {
    signIn("reviewer", REVIEWER, { "GET /integrations/IG-AAA111": () => jsonResponse({ error: { code: "not_found", message: "Integration IG-AAA111 was not found." } }, 404) });
    renderApp("/integrations/IG-AAA111");
    expect(await screen.findByRole("alert")).toHaveTextContent("was not found");
  });
});

describe("webhooks", () => {
  const endpoints = { "GET /webhooks": () => page([{ endpoint_uid: "WH-OUT001", name: "ERP listener", direction: "outbound", url: "https://hooks.example.com/atlas", events: ["alert.created"], secret_version: 1, status: "active", consecutive_failures: 0, disabled_reason: null, receive_url: null }]),
    "GET /webhooks/events": () => jsonResponse({ items: [{ event_type: "alert.created", version: "1", description: "x", emitted: true }, { event_type: "forecast.updated", version: "1", description: "y", emitted: false }] }) };

  it("shows a new signing secret once and only offers events that are actually emitted", async () => {
    signIn("admin", ADMIN, { ...endpoints, "POST /webhooks": () => jsonResponse({ endpoint_uid: "WH-OUT002", name: "Second", direction: "outbound", url: "https://hooks.example.com/b", events: ["alert.created"], secret_version: 1, status: "active", consecutive_failures: 0, disabled_reason: null, receive_url: null, secret: "whsec_SYNTHETIC_ONE_TIME" }, 201) });
    renderApp("/integrations/webhooks");
    const form = await screen.findByRole("form", { name: "Create webhook" });
    expect(await within(form).findByLabelText("alert.created")).toBeChecked();
    expect(within(form).queryByLabelText("forecast.updated")).toBeNull();
    await userEvent.type(within(form).getByLabelText("Name"), "Second");
    await userEvent.type(within(form).getByLabelText(/Destination/), "https://hooks.example.com/b");
    await userEvent.click(within(form).getByRole("button", { name: "Create endpoint" }));
    expect(await screen.findByLabelText("Signing secret")).toHaveTextContent("whsec_SYNTHETIC_ONE_TIME");
    await userEvent.click(screen.getAllByRole("button", { name: "Disable" })[0]!);
    await waitFor(() => expect(screen.queryByLabelText("Signing secret")).toBeNull()); // the next action clears it
  });

  it("shows delivery history and redelivers a dead-lettered event", async () => {
    signIn("admin", ADMIN, { ...endpoints,
      "GET /webhooks/WH-OUT001/deliveries": () => page([{ delivery_uid: "WD-1", event_id: "e1", event_type: "alert.created", status: "dead", attempts: 6, next_attempt_at: null, last_status_code: null, last_error: "HTTP 503", created_at: null, delivered_at: null }]),
      "POST /webhooks/deliveries/WD-1/redeliver": () => jsonResponse({ delivery_uid: "WD-1", status: "queued" }, 202) });
    renderApp("/integrations/webhooks");
    await userEvent.click(await screen.findByRole("button", { name: "Deliveries of ERP listener" }));
    expect(await screen.findByText("HTTP 503")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Redeliver" }));
    expect(await screen.findByText("Redelivery queued.")).toBeInTheDocument();
  });

  it("does not offer mutation controls to a reviewer", async () => {
    signIn("reviewer", REVIEWER, endpoints);
    renderApp("/integrations/webhooks");
    expect(await screen.findByText("ERP listener")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rotate secret" })).toBeNull();
    expect(screen.queryByRole("form", { name: "Create webhook" })).toBeNull();
  });
});

describe("API key lifecycle", () => {
  const keyRow = { key_uid: "AK-1", name: "ci key", prefix: "atl_k_AbCdEf", role: "viewer", scopes: [], expires_at: "2027-01-01T00:00:00Z", revoked_at: null, last_used_at: null, use_count: 0, rotated_from: null };
  const base = { "GET /service-accounts": () => page([{ sa_uid: "SA-1", name: "Reporting bot", description: null, role: "viewer", status: "active" }]), "GET /service-accounts/SA-1/keys": () => jsonResponse({ items: [keyRow] }) };

  it("creates a key whose secret is shown once, and never lists the secret afterwards", async () => {
    signIn("admin", ADMIN, { ...base, "POST /service-accounts/SA-1/keys": () => jsonResponse({ ...keyRow, key_uid: "AK-2", secret: "atl_k_SYNTHETIC_FULL_SECRET_VALUE" }, 201) });
    renderApp("/integrations/keys");
    await userEvent.click(await screen.findByRole("button", { name: "Keys of Reporting bot" }));
    const form = await screen.findByRole("form", { name: "Create API key" });
    await userEvent.type(within(form).getByLabelText("Key name"), "deploy key");
    await userEvent.click(within(form).getByRole("button", { name: "Create key" }));
    expect(await screen.findByLabelText("New API key")).toHaveTextContent("atl_k_SYNTHETIC_FULL_SECRET_VALUE");
    expect(calls.find((c) => c.method === "POST" && c.path.endsWith("/keys"))?.body).toEqual({ name: "deploy key", ttl_days: 90 });
    const listing = screen.getByRole("region", { name: "Keys" });
    expect(listing).not.toHaveTextContent("SYNTHETIC_FULL_SECRET");
    expect(window.localStorage.length + window.sessionStorage.length).toBeLessThanOrEqual(1); // only the session token; the key is never stored
    expect(JSON.stringify([...Object.entries(window.sessionStorage)])).not.toContain("SYNTHETIC_FULL_SECRET");
  });

  it("revokes and rotates keys, explaining the grace period", async () => {
    signIn("admin", ADMIN, { ...base,
      "POST /service-accounts/keys/AK-1/revoke": () => jsonResponse({ ...keyRow, revoked_at: "2026-10-18T00:00:00Z" }),
      "POST /service-accounts/keys/AK-1/rotate": () => jsonResponse({ ...keyRow, key_uid: "AK-3", secret: "atl_k_SYNTHETIC_ROTATED" }, 201) });
    renderApp("/integrations/keys");
    await userEvent.click(await screen.findByRole("button", { name: "Keys of Reporting bot" }));
    await userEvent.click(await screen.findByRole("button", { name: "Rotate ci key" }));
    expect(await screen.findByText(/keeps working for 24 hours/)).toBeInTheDocument();
    expect(screen.getByLabelText("New API key")).toHaveTextContent("atl_k_SYNTHETIC_ROTATED");
    await userEvent.click(screen.getByRole("button", { name: "Revoke ci key" }));
    expect(await screen.findByText(/stopped working immediately/)).toBeInTheDocument();
    expect(screen.queryByLabelText("New API key")).toBeNull();
  });

  it("surfaces a server refusal, e.g. an administrator role for a service account", async () => {
    signIn("admin", ADMIN, { ...base, "POST /service-accounts": () => jsonResponse({ error: { code: "validation_error", message: "Role must be one of: viewer, editor, reviewer. Service accounts are never administrators." } }, 422) });
    renderApp("/integrations/keys");
    const form = await screen.findByRole("form", { name: "Create service account" });
    await userEvent.type(within(form).getByLabelText("Name"), "Root bot");
    await userEvent.click(within(form).getByRole("button", { name: "Create account" }));
    expect(await screen.findByText(/never administrators/)).toBeInTheDocument();
    expect(within(form).queryByRole("option", { name: "admin" })).toBeNull();
  });
});
