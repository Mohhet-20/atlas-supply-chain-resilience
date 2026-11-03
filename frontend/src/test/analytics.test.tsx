import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { BarChart, KpiCard, LineChart } from "../components/analytics/Bits";
import type { Metric } from "../lib/analyticsTypes";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

// All data below is SYNTHETIC test data.
type Handler = (url: URL) => Response;
let calls: { path: string; search: string }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = new URL(String(input));
    const path = url.pathname.replace(/^\/api\/v1/, "");
    calls.push({ path, search: url.search });
    const h = routes[path];
    return Promise.resolve(h ? h(url) : jsonResponse({ error: { code: "not_found", message: `no route ${path}` } }, 404));
  });
}
const renderApp = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
beforeEach(() => tokenStore.clear());
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const me = (role: string) => () => jsonResponse({ actor: "tester", role, authenticated: true, auth_required: true, writes_enabled: true });

const meta = { registry_version: "an-1", generated_at: "2026-11-02T00:00:00Z", timezone: "UTC", window: { days: 30, start: "2026-10-03", end: "2026-11-02", previous_start: "2026-09-03" }, applied_filters: {}, ignored_filters: [], cached: false, scope: "the caller's organisation, as visible to the role" };
const metric = (id: string, over: Partial<Metric> = {}): Metric => ({
  id, label: id.replaceAll("_", " ").toLowerCase(), area: "supplier_risk", unit: "count", value: 3, state: "ok", reason: null, as_of: null, direction: "min", time_basis: "now",
  comparison: null, breakdown: null, filters_honoured: [], drilldown: null, warnings: [], ...over,
});
const filters = { windows: [7, 30, 90], regions: ["EMEA"], categories: ["Electronics"], risk_classes: ["LOW", "UNKNOWN"], unsupported: ["organisation"] };
const overview = (metrics: Metric[], findings: unknown[] = []) => ({ meta, metrics, findings, unavailable: [], findings_note: "Findings are fixed rules, not alerts." });
const trends = { meta, supplier_risk: { points: [{ date: "2026-11-01", suppliers_in_scope: 4, classified: 3, high_or_critical: 1, unknown: 1 }], basis: "Reconstructed from assessment history" }, activity: { dates: ["2026-11-01"], series: { scenario_runs: [2] }, basis: "runs" } };
const base = (extra: Record<string, Handler> = {}) => ({
  "/registry/me": me("viewer"), "/analytics/filters": () => jsonResponse(filters), "/analytics/trends": () => jsonResponse(trends),
  "/analytics/activity": () => jsonResponse({ items: [], total: 0, limit: 8, offset: 0, audit_included: false, note: "n" }), ...extra,
});

describe("analytics components", () => {
  it("shows unknown as Unknown, never zero", () => {
    render(<MemoryRouter><KpiCard m={metric("RISK_UNASSESSED_SUPPLIERS", { value: null, state: "no_data", reason: "No suppliers exist." })} /></MemoryRouter>);
    const card = screen.getByTestId("kpi-RISK_UNASSESSED_SUPPLIERS");
    expect(within(card).getByText("Unknown")).toBeInTheDocument();
    expect(within(card).getByText("No suppliers exist.")).toBeInTheDocument();
    expect(card.textContent).not.toMatch(/\b0\b/);
  });
  it("shows a real zero as 0", () => {
    render(<MemoryRouter><KpiCard m={metric("RISK_ALERTS_OPEN", { value: 0 })} /></MemoryRouter>);
    expect(within(screen.getByTestId("kpi-RISK_ALERTS_OPEN")).getByText("0")).toBeInTheDocument();
  });
  it("states change direction in words and uses 'changed' for neutral metrics", () => {
    render(<MemoryRouter>
      <KpiCard m={metric("A", { comparison: { previous: 2, change: 1, change_pct: 50, verdict: "worse", note: null } })} />
      <KpiCard m={metric("B", { direction: "neutral", comparison: { previous: 2, change: 1, change_pct: null, verdict: "changed", note: "Previous value is zero" } })} />
    </MemoryRouter>);
    expect(within(screen.getByTestId("change-A")).getByText("worse")).toBeInTheDocument();
    expect(within(screen.getByTestId("change-B")).getByText("changed")).toBeInTheDocument();
    expect(screen.getByTestId("change-B").textContent).not.toContain("%");
  });
  it("gives every chart a table alternative", async () => {
    render(<><BarChart title="Suppliers" rows={[{ label: "High", value: 2 }]} /><LineChart title="Trend" unit="n" dates={["d1", "d2"]} series={[{ name: "s", values: [1, null] }]} /></>);
    const toggles = screen.getAllByText("Show as table");
    expect(toggles).toHaveLength(2);
    await userEvent.click(toggles[0]!);
    expect(screen.getAllByRole("table").length).toBeGreaterThan(0);
  });
});

describe("analytics workspace", () => {
  it("renders the overview with findings labelled as findings", async () => {
    mockApi(base({ "/analytics/overview": () => jsonResponse(overview([metric("RISK_HIGH_OR_CRITICAL", { value: 2 }), metric("RISK_ASSESSED_SUPPLIERS", { breakdown: { HIGH: 2, LOW: 1, UNKNOWN: 1 } })], [{ metric: "RISK_HIGH_OR_CRITICAL", severity: "high", text: "2 suppliers are high or critical", link: "/analytics/suppliers", kind: "finding" }])) }));
    renderApp("/analytics");
    expect(await screen.findByText(/Analytical findings/)).toBeInTheDocument();
    expect(screen.getByText(/2 suppliers are high or critical/)).toBeInTheDocument();
    expect(screen.getByText(/not operational alerts/)).toBeInTheDocument();
    expect(screen.getAllByText(/Unknown \(not assessed\)/).length).toBeGreaterThan(0);
  });
  it("puts filters in the URL and sends them to the API", async () => {
    mockApi(base({ "/analytics/overview": () => jsonResponse(overview([metric("RISK_HIGH_OR_CRITICAL")])) }));
    renderApp("/analytics?region=EMEA&window_days=7");
    await screen.findByText(/Analytical findings/);
    const ov = calls.filter((c) => c.path === "/analytics/overview").pop();
    expect(ov?.search).toContain("region=EMEA");
    expect(ov?.search).toContain("window_days=7");
    expect(ov?.search).not.toMatch(/organi[sz]ation|tenant/);
  });
  it("shows an error state with retry when the API fails", async () => {
    mockApi(base({ "/analytics/overview": () => jsonResponse({ error: { code: "internal", message: "boom" } }, 500) }));
    renderApp("/analytics");
    expect(await screen.findByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
  it("shows empty states for alerts and activity", async () => {
    mockApi(base({ "/analytics/alerts": () => jsonResponse({ items: [], total: 0, limit: 15, offset: 0, meta, priority: "severity then newest" }) }));
    renderApp("/analytics/alerts");
    expect(await screen.findByText("No open alerts")).toBeInTheDocument();
    expect(screen.getByText("No activity yet")).toBeInTheDocument();
  });
  it("hides export buttons from viewers and shows them to editors", async () => {
    const reg = { registry_version: "an-1", metrics: [], unavailable: [{ id: "X", label: "On-time delivery rate", reason: "no actual delivery data" }] };
    const health = { registry_version: "an-1", windows: [7, 30, 90], default_window: 30, export_areas: [], cache_ttl_seconds: 5, export_max_rows: 5000, unavailable: [], scope: "Single deployment" };
    mockApi(base({ "/analytics/metrics": () => jsonResponse(reg), "/analytics/health": () => jsonResponse(health) }));
    renderApp("/analytics/reports");
    expect(await screen.findByText(/Exports need the editor role/)).toBeInTheDocument();
    expect(await screen.findByText(/On-time delivery rate/)).toBeInTheDocument();
    cleanup();
    mockApi(base({ "/registry/me": me("editor"), "/analytics/metrics": () => jsonResponse(reg), "/analytics/health": () => jsonResponse(health) }));
    renderApp("/analytics/reports");
    expect(await screen.findByRole("button", { name: /All metrics \(CSV\)/ })).toBeInTheDocument();
  });
});
