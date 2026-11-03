import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { AlertDetail, AlertRow } from "../lib/alertingTypes";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

// All data below is SYNTHETIC test data.
type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; search: string; body: unknown }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const method = init?.method ?? "GET";
    calls.push({ method, path, search: url.search, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const h = routes[`${method} ${path}`] ?? routes[path];
    return Promise.resolve(h ? h(url, init) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
  });
}
const renderApp = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
beforeEach(() => tokenStore.clear());
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const me = (role: string) => () => jsonResponse({ actor: `${role}-user`, role, authenticated: true, auth_required: true, writes_enabled: true });

const row = (over: Partial<AlertRow> = {}): AlertRow => ({
  alert_uid: "AL-AAAA1111", title: "Facility F-1 temp above 50", summary: "Observed 60 C", source: "event", detector: "event_threshold", basis: "observed", severity: "HIGH", priority: "P2", status: "open",
  entity_type: "facility", entity_id: "F-1", entity_label: "Plant 1", link: null, assignee: null, rule_uid: "AR-1", rule_name: "High temp", rule_version: 1, last_rule_version: 1,
  first_seen_at: "2026-11-02T10:00:00Z", last_seen_at: "2026-11-02T10:05:00Z", occurrence_count: 2, due_ack_at: "2026-11-02T10:10:00Z", due_resolve_at: "2026-11-02T12:00:00Z",
  escalation_level: 0, acknowledged_at: null, acknowledged_by: null, resolved_at: null, resolution_mode: null, reopened_count: 0, version: 3, ...over,
});
const detail = (over: Partial<AlertDetail> = {}): AlertDetail => ({
  ...row(), rule_definition_at_creation: null, can_transition_to: ["acknowledged", "escalated", "resolved"], occurrences: [{ observed_at: "2026-11-02T10:00:00Z", value: 60, event_id: 1, detail: null }],
  transitions: [{ from: null, to: "open", actor: "system", mode: "automatic", reason: null, at: "2026-11-02T10:00:00Z" }], notes: [], escalations: [], related: [], suppression: null, deliveries: null, ...over,
});
const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 15, offset: 0 });
const base = (extra: Record<string, Handler> = {}) => ({
  "/registry/me": me("viewer"), "/alerting/notifications/count": () => jsonResponse({ unread: 2 }), ...extra,
});

describe("alert list", () => {
  it("shows severity as text, the basis of each alert and the unread count", async () => {
    mockApi(base({ "/alerting/alerts": () => jsonResponse(page([row(), row({ alert_uid: "AL-BBBB2222", title: "Port scenario", basis: "hypothetical", severity: "MEDIUM" })])) }));
    renderApp("/alerting");
    expect(await screen.findByText("Facility F-1 temp above 50")).toBeInTheDocument();
    expect(screen.getByText("▲ high")).toBeInTheDocument();
    expect(screen.getByText("Hypothetical (scenario, not a live event)")).toBeInTheDocument();
    expect(await screen.findByText("Notifications (2 unread)")).toBeInTheDocument();
  });
  it("does not treat an empty list as proof that nothing is wrong", async () => {
    mockApi(base({ "/alerting/alerts": () => jsonResponse(page([])) }));
    renderApp("/alerting");
    expect(await screen.findByText(/not evidence that nothing is wrong/)).toBeInTheDocument();
  });
  it("sends filters to the server and resets to the first page", async () => {
    mockApi(base({ "/alerting/alerts": () => jsonResponse(page([row()])) }));
    renderApp("/alerting");
    await screen.findByText("Facility F-1 temp above 50");
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "CRITICAL");
    await waitFor(() => expect(calls.some((c) => c.path === "/alerting/alerts" && c.search.includes("severity=CRITICAL") && c.search.includes("live=true"))).toBe(true));
  });
  it("reports an API failure instead of an empty list", async () => {
    mockApi(base({ "/alerting/alerts": () => jsonResponse({ error: { code: "server_error", message: "boom" } }, 500) }));
    renderApp("/alerting");
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
  });
});

describe("alert detail", () => {
  it("gives viewers a read-only page without response controls or delivery history", async () => {
    mockApi(base({ "/alerting/alerts/AL-AAAA1111": () => jsonResponse(detail()) }));
    renderApp("/alerting/alerts/AL-AAAA1111");
    expect(await screen.findByText(/can read this alert but not change it/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resolve" })).not.toBeInTheDocument();
    expect(screen.queryByText("Notification delivery")).not.toBeInTheDocument();
  });
  it("requires a reason to resolve and sends the version being acted on", async () => {
    tokenStore.set("t", false);
    mockApi(base({
      "/registry/me": me("editor"), "/alerting/alerts/AL-AAAA1111": () => jsonResponse(detail()),
      "POST /alerting/alerts/AL-AAAA1111/resolve": () => jsonResponse({ alert_uid: "AL-AAAA1111", status: "resolved", version: 4 }),
    }));
    renderApp("/alerting/alerts/AL-AAAA1111");
    const resolve = await screen.findByRole("button", { name: "Resolve" });
    await userEvent.click(resolve);
    expect(await screen.findByRole("alert")).toHaveTextContent(/reason first/);
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await userEvent.type(screen.getByLabelText(/Reason/), "sensor recalibrated");
    await userEvent.click(resolve);
    await waitFor(() => expect(calls.find((c) => c.path.endsWith("/resolve"))?.body).toEqual({ expected_version: 3, reason: "sensor recalibrated" }));
  });
  it("shows a conflict from the server rather than hiding it", async () => {
    tokenStore.set("t", false);
    mockApi(base({
      "/registry/me": me("editor"), "/alerting/alerts/AL-AAAA1111": () => jsonResponse(detail()),
      "POST /alerting/alerts/AL-AAAA1111/acknowledge": () => jsonResponse({ error: { code: "version_conflict", message: "The alert changed (now version 4); reload before acting." } }, 409),
    }));
    renderApp("/alerting/alerts/AL-AAAA1111");
    await userEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/now version 4/);
  });
  it("labels hypothetical alerts, hides close from editors and shows deliveries to reviewers", async () => {
    tokenStore.set("t", false);
    mockApi(base({
      "/registry/me": me("reviewer"),
      "/alerting/alerts/AL-AAAA1111": () => jsonResponse(detail({ basis: "hypothetical", status: "resolved", can_transition_to: ["closed", "open"], deliveries: [{ delivery_uid: "D1", channel: "in_app", recipient: "editor-user", kind: "initial", status: "DELIVERED", attempts: 1, max_attempts: 3, next_attempt_at: null, last_error: null, created_at: "2026-11-02T10:00:00Z", attempt_log: [] }] })),
    }));
    renderApp("/alerting/alerts/AL-AAAA1111");
    expect(await screen.findByText(/not a live disruption/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
    expect(await screen.findByText("Notification delivery")).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "Deliveries" })).getByText("delivered")).toBeInTheDocument();
  });
});

describe("notifications", () => {
  it("lists my notifications, marks one read and explains unavailable channels", async () => {
    mockApi(base({
      "/alerting/notifications": () => jsonResponse(page([{ id: 7, alert_uid: "AL-AAAA1111", kind: "initial", severity: "HIGH", source: "event", title: "Temp high", body: "b", created_at: "2026-11-02T10:00:00Z", read: false, read_at: null }])),
      "POST /alerting/notifications/7/read": () => jsonResponse({ id: 7, read: true }),
      "/alerting/preferences": () => jsonResponse({ in_app_enabled: true, min_severity: "LOW", quiet_start_hour: null, quiet_end_hour: null, timezone: "UTC", channels: { in_app: { available: true }, email: { available: false, reason: "No email transport is configured." } }, note: "CRITICAL alerts ignore quiet hours." }),
    }));
    renderApp("/alerting/inbox");
    const item = await screen.findByTestId("note-7");
    await userEvent.click(within(item).getByRole("button", { name: "Mark read" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/alerting/notifications/7/read")).toBe(true));
    expect(await screen.findByText(/email: not available — No email transport is configured/)).toBeInTheDocument();
  });
});

describe("operations", () => {
  const an = { window_days: 30, alerts_in_window: 0, definitions: {}, by_severity: {}, by_source: {}, by_basis: {}, by_state_now: {}, acknowledgement: { n: 0, mean_minutes: null, median_minutes: null, unacknowledged: 0 }, resolution: { n: 0, mean_minutes: null, median_minutes: null, unresolved: 0 }, sla: { acknowledgement: { met: 0, missed: 0, not_yet_due: 0, met_pct: null }, resolution: { met: 0, missed: 0, not_yet_due: 0, met_pct: null } }, unresolved_age: {} };
  const health = { tick_seconds: 0, live_alerts: 0, not_supported: ["email notifications"] };
  it("shows unknown rather than zero when there is nothing to measure, and hides engine controls from viewers", async () => {
    mockApi(base({
      "/alerting/sla": () => jsonResponse({ items: [], note: "wall-clock", counts: { overdue: 0, due_soon: 0, on_track: 0 } }),
      "/alerting/analytics": () => jsonResponse(an), "/alerting/health": () => jsonResponse(health),
    }));
    renderApp("/alerting/operations");
    expect((await screen.findAllByText(/mean unknown min/)).length).toBe(2);
    expect(screen.getAllByText(/target met: unknown/).length).toBe(2);
    expect(screen.queryByRole("button", { name: "Evaluate rules now" })).not.toBeInTheDocument();
    expect(await screen.findByText("email notifications")).toBeInTheDocument();
  });
  it("lets a reviewer run an evaluation and reports the result", async () => {
    tokenStore.set("t", false);
    mockApi(base({
      "/registry/me": me("reviewer"), "/alerting/sla": () => jsonResponse({ items: [], note: "n", counts: { overdue: 1, due_soon: 0, on_track: 0 } }),
      "/alerting/analytics": () => jsonResponse(an), "/alerting/health": () => jsonResponse(health),
      "POST /alerting/evaluate": () => jsonResponse({ created: 2, updated: 0, rules: [] }),
    }));
    renderApp("/alerting/operations");
    await userEvent.click(await screen.findByRole("button", { name: "Evaluate rules now" }));
    expect(await screen.findByText(/Evaluation finished.*"created":2/)).toBeInTheDocument();
  });
});

describe("rules", () => {
  it("shows the problems the server found and does not create an invalid rule", async () => {
    tokenStore.set("t", false);
    mockApi(base({
      "/registry/me": me("reviewer"), "/alerting/rules": () => jsonResponse(page([])), "/alerting/policies": () => jsonResponse({ items: [] }),
      "/alerting/health": () => jsonResponse({ detectors: [{ key: "event_threshold", label: "Event threshold", available: true, meaning: "m", missing: "x" }] }),
      "POST /alerting/rules/validate": () => jsonResponse({ valid: false, problems: ["params.threshold: required"] }),
    }));
    renderApp("/alerting/rules");
    await userEvent.type(await screen.findByLabelText("Name"), "My rule");
    await userEvent.click(screen.getByRole("button", { name: "Create rule" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("params.threshold: required");
    expect(calls.some((c) => c.method === "POST" && c.path === "/alerting/rules")).toBe(false);
  });
  it("tells viewers that creating rules needs the reviewer role", async () => {
    mockApi(base({ "/alerting/rules": () => jsonResponse(page([])), "/alerting/policies": () => jsonResponse({ items: [] }), "/alerting/health": () => jsonResponse({ detectors: [] }) }));
    renderApp("/alerting/rules");
    expect(await screen.findByText(/needs the reviewer role/)).toBeInTheDocument();
  });
});
