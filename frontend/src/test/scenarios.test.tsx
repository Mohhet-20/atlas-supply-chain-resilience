import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { tokenStore } from "../lib/registryApi";
import type { Comparison, Impact, ImpactDetail, Run, RunSummary, Scenario } from "../lib/scenarioTypes";
import { jsonResponse } from "./fixtures";

type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; search: URLSearchParams; body?: Record<string, unknown> }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/v1/, "");
    calls.push({ method, path, search: url.searchParams, body: init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined });
    const h = routes[`${method} ${path}`];
    return Promise.resolve(h ? h(url, init) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
  });
}
const renderApp = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
beforeEach(() => tokenStore.clear());
afterEach(() => vi.restoreAllMocks());

const me = (role: string, writes = true) => () => jsonResponse({ actor: "tester", role, authenticated: true, auth_required: true, writes_enabled: writes });
const DISC = "Simulated, hypothetical analysis.";
const assumptions = { downstream_depth: 3, upstream_depth: 1, include_upstream: false, include_ownership: false, include_alternatives: true, include_proposed_alternatives: false, as_of_date: null, assume_no_buffer: true, notes: null };
const LIMITS = { engine_version: "sim-1", config_version: "sc-1", max_events: 25, max_targets_per_event: 50, max_depth: 4, max_nodes: 500, max_edges: 1000, time_budget_seconds: 20, max_active_runs: 3, client_overridable: false, default_assumptions: assumptions, unsupported: { product: "Products have no capacity." }, kind: "limits" };
const page = <T,>(items: T[], total = items.length) => ({ items, total, limit: 25, offset: 0 });
const scenario = (over: Partial<Scenario> = {}): Scenario => ({
  id: 1, scenario_uid: "SC-1", name: "Alpha outage", description: null, scenario_type: "SUPPLIER", template_key: null, status: "DRAFT", version: 1, assumptions, config_version: "sc-1",
  notes: null, owner: "tester", updated_by: "tester", created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-23T10:00:00Z", archived_at: null, validated_version: null,
  validation_current: false, run_count: 0, events: [], event_count: 0, disclaimer: DISC, ...over,
});
const summary = (over: Partial<RunSummary> = {}): RunSummary => ({
  entities_affected: 3, direct: 1, indirect: 2, downstream: 2, upstream: 0, by_type: {}, by_exposure_level: {}, by_quantification: {}, affected_relationships: 2, affected_products: 1,
  affected_facilities: 1, affected_suppliers: 1, capacity_at_risk_by_unit: {}, capacity_at_risk_note: "", relationships_with_unknown_capacity: 1, facility_capacity_lost_by_unit: {},
  alternatives_total: 0, entities_with_alternatives: 0, by_event: [], interpretation: "", ...over,
});
const run = (over: Partial<Run> = {}): Run => ({
  id: 5, run_uid: "SR-5", scenario_id: 1, scenario_name: "Alpha outage", scenario_version: 1, status: "COMPLETED", requested_by: "tester", queued_at: "2026-10-23T10:00:00Z", started_at: null,
  finished_at: null, duration_ms: 12, engine_version: "sim-1", config_hash: "c", definition_hash: "d", graph_hash: "abcdef0123456789abcdef", graph_snapshot: {}, entities_examined: 3,
  entities_affected: 3, direct_count: 1, indirect_count: 2, truncated: false, truncation_reasons: [], cancel_requested: false, complete: true, warnings: [], missing_data: [], failures: [],
  error: null, assumptions, summary: summary(), disclaimer: DISC, ...over,
});
const impact = (over: Partial<Impact> = {}): Impact => ({
  id: 1, entity_type: "FACILITY", entity_id: 2, entity_code: "DEMO-FAC-1", entity_name: "Plant One", impact_kind: "DOWNSTREAM", depth: 1, event_uids: ["e1"], quantification: "UNQUANTIFIED",
  exposure_level: "UNQUANTIFIED", loss_pct: null, loss_pct_lower_bound: null, capacity_lost_value: null, capacity_unit: null, added_lead_time_days: null, path_count: 1, primary_path: [],
  parent_edges: [], rules: [], uncertainty: ["Supply share is unknown."], explanation: "Receives from the disrupted supplier.", potential_exposure: true, ...over,
});
const detail = (i: Impact): ImpactDetail => ({ impact: i, path: [], other_paths: 0, events: [{ event_type: "SUPPLIER_UNAVAILABLE", label: null }], rules_applied: ["R1"], note: "Potential exposure only." }) as unknown as ImpactDetail;
const runRoutes = (r: Run, items: Impact[] = [impact()]): Record<string, Handler> => ({
  "GET /registry/me": me("viewer"),
  [`GET /scenarios/runs/${r.id}`]: () => jsonResponse(r),
  [`GET /scenarios/runs/${r.id}/impacts`]: () => jsonResponse({ ...page(items), run_status: r.status, complete: r.complete }),
  [`GET /scenarios/runs/${r.id}/edges`]: () => jsonResponse(page([])),
  [`GET /scenarios/runs/${r.id}/impacts/FACILITY/2`]: () => jsonResponse(detail(items[0] ?? impact())),
});

describe("scenario library", () => {
  it("shows a loading state then an empty state", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /scenarios": () => jsonResponse(page([])), "GET /scenarios/templates": () => jsonResponse({ templates: [], disclaimer: DISC }) });
    renderApp("/scenarios");
    expect(await screen.findByText(/no scenarios/i)).toBeInTheDocument();
  });
  it("lists scenarios", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /scenarios": () => jsonResponse(page([scenario()])) });
    renderApp("/scenarios");
    expect(await screen.findByText("Alpha outage")).toBeInTheDocument();
  });
  it("shows an error state when the API fails", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /scenarios": () => jsonResponse({ error: { code: "boom", message: "Server broke" } }, 500) });
    renderApp("/scenarios");
    expect(await screen.findByText(/server broke/i)).toBeInTheDocument();
  });
  it("shows an unauthorized message", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /scenarios": () => jsonResponse({ error: { code: "unauthorized", message: "Sign in required" } }, 401) });
    renderApp("/scenarios");
    expect(await screen.findByText(/sign in required/i)).toBeInTheDocument();
  });
});

describe("scenario editor", () => {
  it("displays validation messages and the hypothetical disclaimer", async () => {
    const s = scenario({ events: [{ id: 9, event_uid: "e1", event_type: "SUPPLIER_UNAVAILABLE", label: null, targets: [], magnitude_pct: null, duration_days: null, recovery_days: null, added_lead_time_days: null, region_label: null, effect: null, validation_status: "INVALID", validation_messages: [{ code: "ENTITY_NOT_FOUND", severity: "ERROR", text: "Target supplier 99 does not exist." }], position: 0 }] as unknown as Scenario["events"], event_count: 1 });
    mockApi({ "GET /registry/me": me("editor"), "GET /scenarios/1": () => jsonResponse(s), "GET /scenarios/limits": () => jsonResponse(LIMITS) });
    renderApp("/scenarios/1");
    expect(await screen.findByText(/target supplier 99 does not exist/i)).toBeInTheDocument();
    expect(screen.getAllByText(/hypothetical|simulat/i).length).toBeGreaterThan(0);
  });
  it("viewers cannot edit", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /scenarios/1": () => jsonResponse(scenario()), "GET /scenarios/limits": () => jsonResponse(LIMITS) });
    renderApp("/scenarios/1");
    await screen.findByText("Alpha outage");
    expect(screen.getByRole("button", { name: /run simulation/i })).toBeDisabled();
  });
});

describe("scenario run results", () => {
  it("labels impacts as potential exposure and explains one", async () => {
    mockApi(runRoutes(run()));
    renderApp("/scenarios/runs/5");
    expect(await screen.findByText("Plant One")).toBeInTheDocument();
    expect(screen.getAllByText(/potential exposure/i).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: /explain plant one/i }));
    expect(await screen.findByText(/receives from the disrupted supplier/i)).toBeInTheDocument();
    expect(screen.getByText(/supply share is unknown/i)).toBeInTheDocument();
  });
  it("shows unknown rather than zero for unquantified loss", async () => {
    mockApi(runRoutes(run()));
    renderApp("/scenarios/runs/5");
    await screen.findByText("Plant One");
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
  });
  it("shows a partial banner", async () => {
    mockApi(runRoutes(run({ status: "PARTIAL", truncated: true, truncation_reasons: ["max_nodes"] })));
    renderApp("/scenarios/runs/5");
    expect(await screen.findByText(/partial results/i)).toBeInTheDocument();
    expect(screen.getByText(/max_nodes/)).toBeInTheDocument();
  });
  it("shows a failed run without results", async () => {
    mockApi(runRoutes(run({ status: "FAILED", error: "engine exploded", summary: null, complete: false })));
    renderApp("/scenarios/runs/5");
    expect(await screen.findByText(/engine exploded/)).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Results" })).not.toBeInTheDocument();
  });
  it("shows a running run with cancel for editors", async () => {
    mockApi({ ...runRoutes(run({ status: "RUNNING", summary: null, complete: false })), "GET /registry/me": me("editor") });
    renderApp("/scenarios/runs/5");
    expect(await screen.findByRole("button", { name: /cancel run/i })).toBeInTheDocument();
  });
  it("shows an empty result", async () => {
    mockApi(runRoutes(run({ entities_affected: 0, summary: summary({ entities_affected: 0 }) }), []));
    renderApp("/scenarios/runs/5");
    expect(await screen.findByText(/no affected entities match/i)).toBeInTheDocument();
  });
  it("network tab has a legend, the table fallback note and a bounded count", async () => {
    mockApi(runRoutes(run()));
    renderApp("/scenarios/runs/5");
    await screen.findByText("Plant One");
    await userEvent.click(screen.getByRole("tab", { name: "Network" }));
    expect(await screen.findByText(/Results tab is the complete/i)).toBeInTheDocument();
  });
  it("handles a missing run", async () => {
    mockApi({ "GET /registry/me": me("viewer") });
    renderApp("/scenarios/runs/77");
    await waitFor(() => expect(screen.getAllByText(/no route|not found/i)[0]).toBeInTheDocument());
  });
});

describe("scenario comparison", () => {
  const cmp = (over: Partial<Comparison> = {}): Comparison => ({
    run_ids: [5, 6], comparable: true, not_comparable_reasons: [], warnings: [{ code: "NETWORK_INPUTS_DIFFER", text: "The network data differed between runs." }], metric_comparability: { counts_and_sets: true, capacity: true, events: true },
    assumption_differences: [], runs: [5, 6].map((id) => ({ run_id: id, run_uid: `SR-${id}`, scenario_id: 1, scenario_version: 1, status: "COMPLETED", entities_affected: id, direct: 1, indirect: 1, affected_products: 0, affected_facilities: 0, affected_relationships: 0, capacity_at_risk_by_unit: {}, relationships_with_unknown_capacity: 0, alternatives_total: 0, missing_data_codes: [], warning_codes: [], truncated: false })),
    entities: { in_all_runs: 0, only_in_some_runs: 0, total: 0, truncated: false, rows: [] }, note: "Comparison note.", ...over,
  } as unknown as Comparison);
  it("shows comparability warnings", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /scenarios/compare": () => jsonResponse(cmp()) });
    renderApp("/scenarios/compare?ids=5,6");
    expect(await screen.findByText(/network data differed/i)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/scenarios/compare")?.search.getAll("run_ids")).toEqual(["5", "6"]);
  });
  it("states when runs are not comparable", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /scenarios/compare": () => jsonResponse(cmp({ comparable: false, not_comparable_reasons: [{ code: "ENGINE_DIFFERS", text: "Engine versions differ." }] })) });
    renderApp("/scenarios/compare?ids=5,6");
    expect(await screen.findByText(/not comparable/i)).toBeInTheDocument();
  });
  it("asks for runs when none are chosen", async () => {
    mockApi({ "GET /registry/me": me("viewer") });
    renderApp("/scenarios/compare");
    expect(await screen.findByText(/choose runs to compare/i)).toBeInTheDocument();
  });
});
