import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { schematicLayout, geoLayout } from "../components/transport/RouteDiagram";
import { tokenStore } from "../lib/registryApi";
import type { NetworkNode } from "../lib/transportTypes";
import { jsonResponse } from "./fixtures";

// All data below is SYNTHETIC test data.
type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; text?: string }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/v1/, "");
    calls.push({ method, path, text: typeof init?.body === "string" ? init.body : undefined });
    const h = routes[`${method} ${path}`];
    return Promise.resolve(h ? h(url, init) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
  });
}
const renderApp = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
beforeEach(() => tokenStore.clear());
afterEach(() => vi.restoreAllMocks());
const me = (role: string, writes = true) => () => jsonResponse({ actor: "tester", role, authenticated: true, auth_required: true, writes_enabled: writes });

const nodes: NetworkNode[] = [1, 2, 3, 4].map((i) => ({ facility_id: i, code: `F${i}`, name: `Hub ${"ABCD"[i - 1]}`, country_or_region: "Testland", coordinates: null }));
const network = (over: Record<string, unknown> = {}) => ({
  network_hash: "a".repeat(64), summary: { lanes: 2, facilities: 4, modes: { ROAD: 2 }, excluded_lanes: 0, issues: 0 }, as_of: null, truncated: false, nodes,
  lanes: [
    { lane_uid: "LN-1", origin_facility_id: 1, destination_facility_id: 2, mode: "ROAD", distance_km: null, duration_hours: 10, cost: null, currency: null, capacity: null, capacity_unit: null, provenance: "SYNTHETIC_DEMO", flags: [] },
  ],
  excluded: [], issues: [{ code: "UNDATED", severity: "warning", lanes: ["LN-1"], detail: "No observation date." }], statement: "Straight lines are not routes.", ...over,
});
const metrics = (ids: number[], modes: string[], over: Record<string, unknown> = {}) => ({
  lane_uids: ids.slice(1).map((_, i) => `LN-${ids[i]}${ids[i + 1]}`), facility_ids: ids, hops: ids.length - 1, transfers: ids.length - 2, modes, distance_km: null,
  duration: { transit_hours: 22, transfer_hours: 0, total_hours: 22, known_hours: 22, status: "COMPLETE", quality: "VERIFIED", missing_lanes: [] },
  cost: { total: 220, currency: "USD", status: "COMPLETE", by_currency: { USD: 220 }, missing_lanes: [], quality: "VERIFIED", scope: "transport charges only" },
  reliability: { value: 0.9, upper_bound: 0.9, status: "COMPLETE", missing_lanes: [], sources: ["PRIOR"], assumption: "independent" },
  risk: { value: null, coverage: 0, facilities_without_score: [1, 2], basis: "x" }, capacity: { bottleneck: null, unit: null, status: "INCOMPLETE" },
  freshness: { oldest_observed_at: null, undated_lanes: [] }, provenance: ["SYNTHETIC_DEMO"], evidence_level: "SYNTHETIC", data_flags: [], disruption_notes: [], ...over,
});
const route = (rank: number, ids: number[], modes: string[], over: Record<string, unknown> = {}) => ({
  rank, metrics: metrics(ids, modes), constraints: [{ constraint: "max_transit_hours", state: "SATISFIED", limit: 30, observed: 22, detail: "Total transit." }],
  score: { composite: 0.1 * rank, parts: [] }, pareto_optimal: rank === 1, overlap_with_best: 1, trade_offs: rank === 1 ? null : { cost: 50, duration: -2, reliability: null, risk: null },
  explanation: { summary: rank === 1 ? "Ranked first for objective FASTEST." : "Ranked behind.", criteria: [{ criterion: "duration", value: 22, direction: "min", normalised: 0, weight: 1, contribution: 0 }], binding_constraints: [], caveats: ["At least one lane is synthetic demonstration data."] }, ...over,
});
const run = (over: Record<string, unknown> = {}) => ({
  run_uid: "RR-AAAA1111", kind: "OPTIMIZE", status: "COMPLETED", result_status: "feasible", requested_by: "tester", origin_facility_id: 1, destination_facility_id: 4, scenario_id: null,
  objective: "FASTEST", algorithm_version: "rt-1", network_hash: "b".repeat(64), scenario_hash: null, queued_at: "2026-11-01T06:00:00Z", started_at: null, finished_at: null,
  duration_ms: 5, error: null, route_count: 2, best_route: ["LN-12"], request: {}, request_hash: "c".repeat(64), network_summary: null,
  routes: [route(1, [1, 2, 4], ["ROAD", "ROAD"]), route(2, [1, 4], ["SEA"])], baseline_routes: null, comparison: null,
  rejections: { count: 0, by_constraint: {}, examples: [], cut_before_completion: {}, cut_note: "", infeasibility: null }, disruptions: null,
  summary: { status: "feasible", objective: "FASTEST", weights: { duration: 1 }, ranking_currency: null, route_count: { feasible: 2, unverifiable: 0, rejected: 0 }, unverifiable: [], pareto_front: [], references: {}, search: { expansions: 9, complete_candidates: 2, pruned_branches: 0, budget: 100, truncated: false, yen_fallback_used: false, max_hops: 6, optimality: "EXACT_WITHIN_HOP_LIMIT" } },
  warnings: ["The network includes synthetic demonstration lanes: results verify the software, not real transport options."], provenance: { lane_provenance: { SYNTHETIC_DEMO: 6 }, as_of: "2026-11-01T06:00:00Z", reliability_sources: ["PRIOR"] },
  reproducibility: { request_hash: "c", algorithm_version: "rt-1", network_hash: "b", scenario_hash: null, overlay_hash: null, as_of: "2026-11-01T06:00:00Z", note: "Replaying reproduces these routes." }, statements: ["Routes are modelled plans, not bookings."], ...over,
});
const base = { "GET /registry/me": me("editor"), "GET /routing/network": () => jsonResponse(network()) };

describe("planning workspace", () => {
  it("shows an empty state when there is no network", async () => {
    mockApi({ ...base, "GET /routing/network": () => jsonResponse(network({ nodes: [], lanes: [] })) });
    renderApp("/transport");
    expect(await screen.findByText("No transport network yet")).toBeInTheDocument();
  });

  it("submits the chosen objective and constraints to the API and opens the run", async () => {
    mockApi({
      ...base,
      "GET /scenarios": () => jsonResponse({ items: [{ id: 7, name: "Hub C closure" }], total: 1, limit: 100, offset: 0 }),
      "POST /routing/optimize": () => jsonResponse(run({ status: "QUEUED", result_status: null }), 202),
      "GET /routing/runs/RR-AAAA1111": () => jsonResponse(run()),
    });
    renderApp("/transport");
    await userEvent.selectOptions(await screen.findByLabelText("Origin facility"), "1");
    await userEvent.selectOptions(screen.getByLabelText("Destination facility"), "4");
    await userEvent.selectOptions(screen.getByLabelText("Optimise for"), "FASTEST");
    await userEvent.type(screen.getByLabelText("Max transit hours"), "30");
    await userEvent.type(screen.getByLabelText("Budget"), "500");
    await userEvent.click(within(screen.getByRole("group", { name: "Prohibited modes" })).getByLabelText("sea"));
    await userEvent.selectOptions(await screen.findByLabelText("Compare with scenario"), "7");
    await userEvent.click(screen.getByRole("button", { name: "Find routes" }));
    expect(await screen.findByRole("heading", { name: "Ranked alternatives" })).toBeInTheDocument();
    const body = JSON.parse(calls.find((c) => c.method === "POST")!.text!);
    expect(body).toMatchObject({ origin_facility_id: 1, destination_facility_id: 4, objective: "FASTEST", scenario_id: 7, constraints: { max_transit_hours: 30, max_cost: 500, currency: "USD" } });
    expect(body.constraints.prohibited_modes).toContain("SEA");
  });

  it("does not let a viewer start a run", async () => {
    mockApi({ ...base, "GET /registry/me": me("viewer") });
    renderApp("/transport");
    await screen.findByLabelText("Origin facility");
    expect(screen.getByRole("button", { name: "Find routes" })).toBeDisabled();
    expect(screen.getByText(/needs the editor role/)).toBeInTheDocument();
  });

  it("shows the server's rejection message", async () => {
    mockApi({ ...base, "POST /routing/optimize": () => jsonResponse({ error: { code: "validation_error", message: "max_cost and currency must be given together" } }, 422) });
    renderApp("/transport");
    await userEvent.selectOptions(await screen.findByLabelText("Origin facility"), "1");
    await userEvent.selectOptions(screen.getByLabelText("Destination facility"), "4");
    await userEvent.click(screen.getByRole("button", { name: "Find routes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("must be given together");
  });
});

describe("run results", () => {
  it("lists ranked alternatives with trade-offs and never shows an unknown value as zero", async () => {
    mockApi({ ...base, "GET /routing/runs/RR-AAAA1111": () => jsonResponse(run()) });
    renderApp("/transport/runs/RR-AAAA1111");
    expect(await screen.findByRole("heading", { name: "Ranked alternatives" })).toBeInTheDocument();
    const rows = within(screen.getByRole("table", { name: "Ranked routes with their trade-offs" })).getAllByRole("row");
    expect(rows.length).toBe(3);
    expect(screen.getAllByText("no score").length).toBeGreaterThan(0);
    expect(screen.getByText("cost +50, duration -2")).toBeInTheDocument();
    expect(screen.getByText(/synthetic demonstration lanes/)).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Show details of route 2"));
    expect(screen.getByRole("heading", { name: /Route 2/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Routes:/ })).toBeInTheDocument();
    expect(screen.getByText("Traceability")).toBeInTheDocument();
  });

  it("explains an infeasible result without relaxing anything", async () => {
    mockApi({
      ...base,
      "GET /routing/runs/RR-AAAA1111": () => jsonResponse(run({
        result_status: "no_feasible_route", routes: [], route_count: 0,
        rejections: { count: 0, by_constraint: {}, examples: [], cut_before_completion: { max_transit_hours: 3 }, cut_note: "", infeasibility: { reasons: ["BOUNDS: every connecting route would exceed a limit."], network_connected: true, near_misses: [], relaxation: "No constraint was relaxed." } },
      })),
    });
    renderApp("/transport/runs/RR-AAAA1111");
    expect(await screen.findByRole("heading", { name: "Why there is no feasible route" })).toBeInTheDocument();
    expect(screen.getByText(/No feasible route/)).toBeInTheDocument();
    expect(screen.getByText("No constraint was relaxed.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Ranked alternatives" })).not.toBeInTheDocument();
  });

  it("separates unverifiable routes from ranked ones", async () => {
    const unv = { metrics: metrics([1, 3, 4], ["AIR", "AIR"]), constraints: [], reasons: ["Lanes without a duration: LN-13."] };
    mockApi({ ...base, "GET /routing/runs/RR-AAAA1111": () => jsonResponse(run({ result_status: "unverifiable", routes: [], summary: { ...run().summary, route_count: { feasible: 0, unverifiable: 1, rejected: 0 }, unverifiable: [unv] } })) });
    renderApp("/transport/runs/RR-AAAA1111");
    expect(await screen.findByRole("heading", { name: /Unverifiable routes \(1\)/ })).toBeInTheDocument();
    expect(screen.getByText(/Lanes without a duration/)).toBeInTheDocument();
    expect(screen.getByText(/No route can be confirmed feasible/)).toBeInTheDocument();
  });

  it("compares baseline with the scenario and says what was not applied", async () => {
    const cmp = {
      status_change: null, baseline_status: "feasible", scenario_status: "feasible", best_route_changed: true, baseline_best: ["LN-13", "LN-34"], scenario_best: ["LN-12", "LN-24"], best_route_deltas: { cost: -100, duration: 16, reliability: null, risk: null },
      baseline_routes_in_scenario: [{ lane_uids: ["LN-13", "LN-34"], state: "UNAVAILABLE", causes: [{ lane_uid: "LN-13", state: "FACILITY_CLOSED", cause: "scenario:S:E1" }] }],
    };
    mockApi({
      ...base,
      "GET /routing/runs/RR-AAAA1111": () => jsonResponse(run({
        scenario_id: 7, scenario_hash: "d".repeat(64), comparison: cmp, baseline_routes: [route(1, [1, 3, 4], ["AIR", "AIR"])],
        disruptions: { scenario: { scenario_id: 7, scenario_uid: "SC-1", name: "Hub C closure", version: 3, definition_hash: "d" }, applied: [], not_applied: [{ source: "scenario:S:E2", event_type: "SUPPLIER_UNAVAILABLE", reason: "Supplier-level events do not identify transport lanes." }], overlay_hash: "o", baseline_unchanged: true },
      })),
    });
    renderApp("/transport/runs/RR-AAAA1111");
    expect(await screen.findByRole("heading", { name: /Hub C closure/ })).toBeInTheDocument();
    expect(screen.getByText("The best route changed.", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("✖ unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Supplier-level events do not identify transport lanes/)).toBeInTheDocument();
    expect(screen.getByText(/never modified/)).toBeInTheDocument();
  });

  it("reports failed and cancelled runs plainly", async () => {
    mockApi({ ...base, "GET /routing/runs/RR-AAAA1111": () => jsonResponse(run({ status: "FAILED", result_status: null, routes: null, error: "compute budget exceeded" })) });
    renderApp("/transport/runs/RR-AAAA1111");
    expect(await screen.findByRole("alert")).toHaveTextContent("compute budget exceeded");
    expect(screen.getByRole("alert")).toHaveTextContent("Nothing was changed");
  });
});

describe("history, network and capabilities", () => {
  it("shows empty history and a populated one", async () => {
    mockApi({ ...base, "GET /routing/runs": () => jsonResponse({ items: [], total: 0, limit: 15, offset: 0 }) });
    renderApp("/transport/runs");
    expect(await screen.findByText("No routing runs yet")).toBeInTheDocument();
    vi.restoreAllMocks();
    mockApi({ ...base, "GET /routing/runs": () => jsonResponse({ items: [run()], total: 1, limit: 15, offset: 0 }) });
    renderApp("/transport/runs");
    expect((await screen.findAllByRole("link", { name: "RR-AAAA1111" })).length).toBeGreaterThan(0);
  });

  it("shows lanes with unknown figures stated, and data-quality findings", async () => {
    mockApi(base);
    renderApp("/transport/network");
    expect(await screen.findByText("Data-quality findings (1)")).toBeInTheDocument();
    expect(screen.getByText("hidden or unknown")).toBeInTheDocument();
    expect(screen.getAllByText("unknown").length).toBeGreaterThan(0);
  });

  it("shows an error with retry", async () => {
    mockApi({ ...base, "GET /routing/network": () => jsonResponse({ error: { code: "boom", message: "Server trouble" } }, 500) });
    renderApp("/transport/network");
    expect(await screen.findByText("Server trouble")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("states capabilities and exclusions", async () => {
    mockApi({
      ...base,
      "GET /routing/health": () => jsonResponse({ algorithm_version: "rt-1", write_api_enabled: true, lanes: 0, runs_by_status: {}, modes: [], limits: { max_lanes: 5000, max_active_runs: 4, time_budget_seconds: 30, max_expansions: 200000, max_hops: 8, max_alternatives: 10, min_reliability_sample: 20, max_bulk_lanes: 200 }, algorithms: ["Dijkstra"], not_supported: ["booking, tendering or carrier integration"], statements: { decision_support: "Routes are modelled plans.", cost: "c", duration: "d", reliability: "r", risk: "k", scenario: "s", tenancy: "t" } }),
      "GET /routing/objectives": () => jsonResponse({ criteria: [], objectives: [{ name: "BALANCED", weights: {}, note: "Risk is excluded." }], normalisation: "Min-max.", tie_break: [] }),
      "GET /routing/constraints": () => jsonResponse({ states: {}, constraints: [{ name: "max_cost + currency", judged_on: "sum", missing_data: "UNVERIFIABLE" }] }),
    });
    renderApp("/transport/about");
    expect(await screen.findByText("booking, tendering or carrier integration")).toBeInTheDocument();
    expect(screen.getByText(/Risk is excluded/)).toBeInTheDocument();
  });
});

describe("diagram layout", () => {
  it("puts the origin first and the destination last and only places valid coordinates", () => {
    const pos = schematicLayout([{ key: "a", label: "a", facility_ids: [1, 2, 4], modes: ["ROAD", "ROAD"], style: "best" }, { key: "b", label: "b", facility_ids: [1, 4], modes: ["SEA"], style: "alt" }]);
    expect(pos.get(1)!.x).toBeLessThan(pos.get(2)!.x);
    expect(pos.get(2)!.x).toBeLessThan(pos.get(4)!.x);
    const map = new Map(nodes.map((n) => [n.facility_id, n]));
    expect(geoLayout([1, 2], map).pos.size).toBe(0);
    map.set(1, { ...map.get(1)!, coordinates: { latitude: 10, longitude: 20, status: "VERIFIED" } });
    map.set(2, { ...map.get(2)!, coordinates: { latitude: 12, longitude: 25, status: "VERIFIED" } });
    const g = geoLayout([1, 2, 3], map);
    expect(g.pos.size).toBe(2);
    expect(g.unplaced).toEqual([3]);
  });
});
