import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

// All data below is SYNTHETIC test data.
type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; text?: string; headers?: HeadersInit }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/v1/, "");
    calls.push({ method, path, text: typeof init?.body === "string" ? init.body : undefined, headers: init?.headers });
    const h = routes[`${method} ${path}`];
    return Promise.resolve(h ? h(url, init) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
  });
}
const renderApp = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
beforeEach(() => tokenStore.clear());
afterEach(() => vi.restoreAllMocks());
const me = (role: string, writes = true) => () => jsonResponse({ actor: "tester", role, authenticated: true, auth_required: true, writes_enabled: writes });

const OBJ = (id: string, name: string, direction = "min", unit = "units") => ({ id, name, direction, unit, definition: `${name} definition`, inputs: [], normalisation: "min-max", missing: `${name} needs inputs`, explanation: "x" });
const catalogue = {
  objectives: [OBJ("COST", "Total modelled cost", "min", "currency"), OBJ("UNMET_DEMAND", "Unmet demand"), OBJ("DELIVERY_DELAY", "Delivery delay"), OBJ("CONCENTRATION", "Supplier concentration", "min", "HHI"), OBJ("DISRUPTION_EXPOSURE", "Outage exposure"), OBJ("WORST_CASE_SERVICE", "Worst-case fulfilment", "max", "fraction")],
  strategies: [{ id: "WEIGHTED_SUM", label: "Weighted sum", note: "One recommended plan." }, { id: "PARETO", label: "Pareto set", note: "No single winner." }, { id: "EPSILON_CONSTRAINT", label: "Primary objective with limits", note: "Minimise one." }],
  stress_kinds: [], scenario_modes: [], constraints: [{ id: "eligibility", kind: "hard", description: "Only eligible suppliers." }, { id: "max_hhi", kind: "soft", description: "Concentration cap." }],
  result_statuses: ["completed", "infeasible"], statements: { decision_support: "A resilience plan is a modelled proposal; ATLAS never places an order." },
};
const health = { algorithm_version: "mo-1", write_api_enabled: true, runs_by_status: {}, limits: { max_active_runs: 3, time_budget_seconds: 40 }, solver: { name: "grid-enumeration", version: "mo-1", type: "APPROXIMATE_GRID", deterministic: true }, algorithms: [], not_supported: ["creating, sending or tracking purchase orders or any automatic execution", "per-organisation isolation (no organisation model)"], statements: {} };
const base = { "GET /registry/me": me("editor"), "GET /resilience/catalogue": () => jsonResponse(catalogue), "GET /resilience/health": () => jsonResponse(health) };

const al = (code: string, q: number, price: number | null = 10) => ({ offer_uid: `OF-${code}`, supplier_id: 1, supplier_code: code, supplier_name: `Supplier ${code}`, country: null, quantity: q, share: q / 60, unit_price: price, lead_time_days: 10 });
const alt = (id: string, over: Record<string, unknown> = {}, vals: Record<string, number | null> = {}) => ({
  id, allocation: [al("S2", 40), al("S3", 20)], total_quantity: 60, values: { COST: 640, UNMET_DEMAND: 0, DELIVERY_DELAY: 10, CONCENTRATION: 0.56, DISRUPTION_EXPOSURE: 40, WORST_CASE_SERVICE: 0.6, ...vals },
  cost: { total_known: 640, purchase: 640, freight: null, shortage_penalty: null, omitted_components: ["freight (not stated by every eligible offer)"], basis: "PURCHASE_ONLY", currency: "USD" }, baseline_fulfilment: 1, scenarios: [], expected_unmet: null,
  pareto: true, score: 0.3, normalised: [0.2], recommended: false, feasible: true, equivalent_plans: [], anchor: null, constraint_checks: [], ...over,
});
const outcome = (name: string, f: number) => ({ name, kind: "SUPPLIER_OUTAGE", probability: null, mode: "FIXED", demand: 60, delivered: 60 * f, supply_lost: 60 * (1 - f), from_stock: 0, recovered: 0, unmet: 60 * (1 - f), fulfilment: f });
const row = (objective: string, name: string, baseline: number | null, candidate: number | null, verdict: string, rel: number | null = null) => ({ objective, name, unit: "u", direction: "min", baseline, candidate, difference: baseline === null || candidate === null ? null : candidate - baseline, relative_pct: rel, verdict, caveat: null });
const run = (over: Record<string, unknown> = {}) => ({
  run_uid: "RO-AAAA1111", status: "COMPLETED", result_status: "completed_with_warnings", requested_by: "other", product_id: 1, destination_facility_id: 2, strategy: "WEIGHTED_SUM", scenario_mode: "FIXED", quantity: 60, unit: "kg", currency: "USD",
  objectives_requested: ["COST", "CONCENTRATION", "WORST_CASE_SERVICE"], stress_count: 1, algorithm_version: "mo-1", snapshot_hash: "d".repeat(64), recommendation_hash: "f".repeat(64), queued_at: "2026-11-01T06:00:00Z", started_at: null, finished_at: null,
  duration_ms: 12, error: null, recommended_id: "C00002", alternative_count: 2, front_size: 2, inexact: false, approval: { state: "PROPOSED", decided_by: null, decided_at: null, rationale: null, executed: false },
  request: {}, request_hash: "e".repeat(64),
  objectives: { registry: [], unavailable: [], enabled: ["COST", "CONCENTRATION", "WORST_CASE_SERVICE"], strategy: "WEIGHTED_SUM", weights: { COST: 0.5, CONCENTRATION: 0.25, WORST_CASE_SERVICE: 0.25 }, normalisation: {} },
  constraints: { hard: [], configured: {}, recommended_checks: [{ id: "demand_balance", passed: true, detail: "total 60 must be within [60, 60]", slack: 0, binding: true }] },
  scenarios: { mode: "FIXED", response_days: null, label: "Fixed plan: the allocation is not changed after the disruption.", weighted: false, results_for: "C00002", expected_unmet: null, worst: "S2 out", definitions: [{ name: "S2 out", kind: "SUPPLIER_OUTAGE", probability: null, affected_offers: ["OF-S2"], not_applied: [] }], outcomes: [outcome("S2 out", 0.6)], baseline_results: [] },
  alternatives: [alt("C00001", {}, { COST: 600, CONCENTRATION: 0.9, WORST_CASE_SERVICE: 0.3 }), alt("C00002", { recommended: true })],
  pareto: { front_size: 2, shown: 2, ids: ["C00001", "C00002"], objectives: ["COST", "CONCENTRATION", "WORST_CASE_SERVICE"], ranges: [], non_discriminating: [], conflicts: [{ objective: "COST", best_plan: "C00001", best_value: 600, values_of_other_objectives_in_that_plan: { CONCENTRATION: 0.9 } }], approximation: null },
  recommendation: { id: "C00002", basis: "WEIGHTED_SUM", allocation: [al("S2", 40), al("S3", 20)], values: { COST: 640, CONCENTRATION: 0.56, WORST_CASE_SERVICE: 0.6 }, decision_support_only: true, statement: "A resilience plan is a modelled proposal. ATLAS never places an order." },
  comparison: { reference: "SUPPLIED_BASELINE", reference_plan: alt("BASELINE"), candidate_id: "C00002", rows: [row("COST", "Total modelled cost", 600, 640, "worse", 6.7), row("CONCENTRATION", "Supplier concentration", 1, 0.56, "better", -44), row("WORST_CASE_SERVICE", "Worst-case fulfilment", 0, 0.6, "better", null)] },
  sensitivity: { base_recommended: "C00002", variants: [{ parameter: "weight", label: "weight of COST x2", result_status: "completed", feasible: true, same_as_base: false, recommended_id: "C00001", allocation: [], values: {} }, { parameter: "demand", label: "demand x1.25", result_status: "completed", feasible: true, same_as_base: true, recommended_id: "C00002", allocation: [], values: {} }], variants_skipped_by_limit: 0 },
  diagnostics: null, explanation: ["Recommended alternative C00002: S2 67%, S3 33%."], warnings: [{ code: "COST_PURCHASE_ONLY", severity: "INFO", message: "Freight is not stated by every eligible offer, so cost is purchase price only." }],
  solver: health.solver, statistics: {}, provenance: {}, reproducibility: { deterministic: true }, statements: {}, ...over,
});
const runs = (...r: unknown[]) => ({ items: r, total: r.length, limit: 15, offset: 0 });
const RUN = "GET /resilience/runs/RO-AAAA1111";
const fill = async () => {
  await userEvent.type(await screen.findByLabelText(/Product \(registry id\)/), "1");
  await userEvent.type(screen.getByLabelText(/Destination facility/), "2");
  await userEvent.type(screen.getByLabelText(/Quantity needed/), "60");
  await userEvent.type(screen.getByLabelText(/^Unit/), "kg");
};

describe("configuration", () => {
  it("lists every objective with its definition and direction, and checks availability without hiding the reason", async () => {
    mockApi({ ...base, "POST /resilience/availability": () => jsonResponse({ objectives: [{ id: "COST", name: "Total modelled cost", direction: "min", unit: "currency", available: true, reason: null }, { id: "WORST_CASE_SERVICE", name: "Worst-case fulfilment", direction: "max", unit: "fraction", available: false, reason: "needs at least one stress scenario" }], eligibility_counts: { ELIGIBLE: 3 }, eligible_capacity_total: 240, demand: 60, opening_stock: { value: null, source: null }, scenarios_applied: [], warnings: [], snapshot_hash: "a".repeat(64) }) });
    renderApp("/resilience");
    await fill();
    expect(screen.getByText("Total modelled cost definition")).toBeInTheDocument();
    expect(screen.getAllByText(/lower is better/).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Check objective availability" }));
    expect(await screen.findByRole("heading", { name: "Objective availability" })).toBeInTheDocument();
    expect(screen.getByText(/unavailable — Worst-case fulfilment: needs at least one stress scenario/)).toBeInTheDocument();
    expect(screen.getByText(/not supplied \(not assumed to be zero\)/)).toBeInTheDocument();
    expect(JSON.parse(calls.find((c) => c.path === "/resilience/availability")!.text!)).toMatchObject({ product_id: 1, quantity: 60, strategy: "WEIGHTED_SUM" });
  });

  it("sends weights, stress, plan constraints and an idempotency key, then opens the run", async () => {
    mockApi({ ...base, "POST /resilience/optimize": () => jsonResponse(run({ status: "QUEUED", result_status: null }), 202), [RUN]: () => jsonResponse(run()) });
    renderApp("/resilience");
    await fill();
    await userEvent.clear(screen.getByLabelText("Weight for Total modelled cost"));
    await userEvent.type(screen.getByLabelText("Weight for Total modelled cost"), "3");
    await userEvent.click(screen.getByLabelText(/Worst-case fulfilment \(/));
    await userEvent.type(screen.getByLabelText(/Max concentration/), "0.7");
    await userEvent.click(screen.getByRole("button", { name: "Add stress scenario" }));
    await userEvent.type(screen.getByLabelText("Stress 1 name"), "Supplier 2 down");
    await userEvent.type(screen.getByLabelText("Stress 1 value"), "2");
    await userEvent.click(screen.getByLabelText(/Include sensitivity analysis/));
    await userEvent.click(screen.getByRole("button", { name: "Run optimisation" }));
    expect(await screen.findByRole("heading", { name: "Recommended alternative C00002" })).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST")!;
    const b = JSON.parse(post.text!);
    expect(b.objectives).toEqual(expect.arrayContaining([{ id: "COST", weight: 3 }, { id: "WORST_CASE_SERVICE", weight: 1 }]));
    expect(b).toMatchObject({ plan_constraints: { max_hhi: 0.7 }, stress: [{ name: "Supplier 2 down", kind: "SUPPLIER_OUTAGE", supplier_id: 2 }], sensitivity: { enabled: true }, scenario_mode: "FIXED" });
    expect((post.headers as Record<string, string>)["Idempotency-Key"]).toMatch(/^ui-/);
  });

  it("shows primary-objective and limit fields only for the epsilon strategy, and response time only for adaptive evaluation", async () => {
    mockApi({ ...base });
    renderApp("/resilience");
    await fill();
    expect(screen.queryByLabelText(/Primary objective/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Response time/)).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText(/^Strategy/), "EPSILON_CONSTRAINT");
    expect(screen.getByLabelText(/Primary objective to optimise/)).toBeInTheDocument();
    expect(screen.getByLabelText("Limit for Supplier concentration (HHI)")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText(/^Evaluation/), "ADAPTIVE");
    expect(screen.getByLabelText(/Response time \(days\)/)).toBeInTheDocument();
  });

  it("does not let a viewer run, and shows the server's rejection message", async () => {
    mockApi({ ...base, "GET /registry/me": me("viewer") });
    renderApp("/resilience");
    await fill();
    expect(screen.getByRole("button", { name: "Run optimisation" })).toBeDisabled();
    expect(screen.getByText(/needs the editor role/)).toBeInTheDocument();
    cleanup();
    vi.restoreAllMocks();
    mockApi({ ...base, "POST /resilience/availability": () => jsonResponse({ error: { code: "validation_error", message: "Unknown supplier 9 in stress scenario 'x'." } }, 422) });
    renderApp("/resilience");
    await fill();
    await userEvent.click(screen.getAllByRole("button", { name: "Check objective availability" }).at(-1)!);
    expect(await screen.findByRole("alert")).toHaveTextContent("Unknown supplier 9");
  });

  it("shows an error state with retry when the catalogue cannot load", async () => {
    mockApi({ "GET /registry/me": me("editor") });
    renderApp("/resilience");
    expect(await screen.findByRole("button", { name: /retry|try again/i })).toBeInTheDocument();
  });
});

describe("run results", () => {
  it("shows the recommendation, a labelled chart with a table of the same numbers, and the baseline comparison in words", async () => {
    mockApi({ ...base, [RUN]: () => jsonResponse(run()) });
    renderApp("/resilience/runs/RO-AAAA1111");
    expect(await screen.findByRole("heading", { name: "Recommended alternative C00002" })).toBeInTheDocument();
    expect(screen.getByText(/completed with warnings/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Trade-off chart of 2 non-dominated alternatives/ })).toBeInTheDocument();
    expect(screen.getByTestId("pt-C00002").tagName.toLowerCase()).toBe("polygon");
    expect(screen.getByTestId("pt-C00001").tagName.toLowerCase()).toBe("circle");
    const t = screen.getByRole("table", { name: /Non-dominated alternatives \(table view/ });
    expect(within(t).getByText(/C00002/)).toBeInTheDocument();
    expect(within(t).getByText("recommended")).toBeInTheDocument();
    const c = screen.getByRole("table", { name: "Baseline comparison" });
    expect(within(c).getByText(/▼ worse/)).toBeInTheDocument();
    expect(within(c).getAllByText(/▲ better/)).toHaveLength(2);
    expect(screen.getByText(/Cost omits: freight/)).toBeInTheDocument();
    expect(screen.getByText(/PROPOSED · not executed/i)).toBeInTheDocument();
    expect(screen.getByText(/inputs snapshot dddddddddddd/)).toBeInTheDocument();
    expect(screen.getByText(/approximate grid search, deterministic/)).toBeInTheDocument();
  });

  it("labels a cost-minimal reference as not the current operation", async () => {
    mockApi({ ...base, [RUN]: () => jsonResponse(run({ comparison: { ...run().comparison, reference: "REFERENCE_COST_MINIMAL" } })) });
    renderApp("/resilience/runs/RO-AAAA1111");
    expect(await screen.findByText(/It is not your current operation/)).toBeInTheDocument();
  });

  it("states fixed vs adaptive, that no probabilities were used, and each scenario outcome", async () => {
    mockApi({ ...base, [RUN]: () => jsonResponse(run()) });
    renderApp("/resilience/runs/RO-AAAA1111");
    const s = await screen.findByRole("heading", { name: "Scenario comparison" });
    expect(s).toBeInTheDocument();
    expect(screen.getByText(/Fixed plan: the allocation is not changed/)).toBeInTheDocument();
    expect(screen.getByText(/not weighted/)).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: /under each stress scenario/ })).getByText("S2 out (worst)")).toBeInTheDocument();
  });

  it("explains infeasibility as diagnostic only, with no recommendation and no approval", async () => {
    mockApi({ ...base, "GET /registry/me": me("reviewer"), [RUN]: () => jsonResponse(run({ result_status: "infeasible", alternatives: [], pareto: null, recommendation: null, recommended_id: null, comparison: null, scenarios: null, sensitivity: null, diagnostics: { eligible_capacity_total: 240, note: "Constraints were not relaxed; figures are diagnostic only." } })) });
    renderApp("/resilience/runs/RO-AAAA1111");
    expect(await screen.findByRole("heading", { name: /Why no plan exists/ })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/Nothing was relaxed/);
    expect(screen.getByText(/Total eligible capacity: 240/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Recommended alternative/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve alternative" })).not.toBeInTheDocument();
  });

  it("says a timeout is not infeasibility", async () => {
    mockApi({ ...base, [RUN]: () => jsonResponse(run({ status: "PARTIAL", result_status: "timed_out", alternatives: [], pareto: null, recommendation: null, comparison: null, scenarios: null, sensitivity: null })) });
    renderApp("/resilience/runs/RO-AAAA1111");
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a finding of infeasibility/);
  });

  it("shows insufficient data without a plan", async () => {
    mockApi({ ...base, [RUN]: () => jsonResponse(run({ result_status: "insufficient_data", alternatives: [], recommendation: null, pareto: null, comparison: null, scenarios: null, sensitivity: null })) });
    renderApp("/resilience/runs/RO-AAAA1111");
    expect(await screen.findByRole("alert")).toHaveTextContent(/Missing data was not assumed/);
  });

  it("lists unavailable objectives with the reason rather than a zero", async () => {
    mockApi({ ...base, [RUN]: () => jsonResponse(run({ objectives: { ...run().objectives, enabled: ["COST", "CONCENTRATION"], unavailable: [{ id: "WORST_CASE_SERVICE", reason: "needs at least one stress scenario" }] } })) });
    renderApp("/resilience/runs/RO-AAAA1111");
    expect(await screen.findByText(/Unavailable and left out: Worst-case fulfilment \(needs at least one stress scenario\)/)).toBeInTheDocument();
  });

  it("hides cost figures from a viewer and says so", async () => {
    const hiddenAlt = (id: string, rec = false) => alt(id, { recommended: rec, allocation: [al("S2", 40, null)] }, { COST: null });
    mockApi({ ...base, "GET /registry/me": me("viewer"), [RUN]: () => jsonResponse(run({ cost_redacted: true, alternatives: [hiddenAlt("C00001"), hiddenAlt("C00002", true)], recommendation: { ...run().recommendation, allocation: [al("S2", 40, null)], values: { COST: null, CONCENTRATION: 0.56 } }, comparison: { ...run().comparison, rows: [row("COST", "Total modelled cost", null, null, "unknown")] } })) });
    renderApp("/resilience/runs/RO-AAAA1111");
    await screen.findByRole("heading", { name: "Recommended alternative C00002" });
    expect(screen.getAllByText("hidden for your role").length).toBeGreaterThan(0);
    expect(screen.queryByText(/640/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve alternative" })).not.toBeInTheDocument();
  });

  it("shows queued, failed and cancelled runs", async () => {
    mockApi({ ...base, [RUN]: () => jsonResponse(run({ status: "QUEUED", result_status: null, alternatives: [], recommendation: null, pareto: null })) });
    renderApp("/resilience/runs/RO-AAAA1111");
    expect(await screen.findByText("Queued…")).toBeInTheDocument();
    cleanup();
    vi.restoreAllMocks();
    mockApi({ ...base, [RUN]: () => jsonResponse(run({ status: "FAILED", result_status: "failed", error: "solver crashed", alternatives: [], recommendation: null, pareto: null })) });
    renderApp("/resilience/runs/RO-AAAA1111");
    expect(await screen.findByText(/This run failed: solver crashed/)).toBeInTheDocument();
  });

  it("lists sensitivity variants, and computes more on demand without claiming they are stored", async () => {
    mockApi({ ...base, [RUN]: () => jsonResponse(run()), "POST /resilience/runs/RO-AAAA1111/sensitivity": () => jsonResponse({ sensitivity: { base_recommended: "C00002", variants: [{ parameter: "demand", label: "demand x1.5", result_status: "completed", feasible: true, same_as_base: false, recommended_id: "C00009", allocation: [], values: {} }], variants_skipped_by_limit: 0 }, note: "not stored" }) });
    renderApp("/resilience/runs/RO-AAAA1111");
    expect(await screen.findByText(/1 of 2 one-at-a-time changes keep the recommendation/)).toBeInTheDocument();
    expect(screen.getByText("no, changes")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Compute sensitivity now" }));
    expect(await screen.findByText("demand x1.5")).toBeInTheDocument();
    expect(screen.getByText(/not stored with the run/)).toBeInTheDocument();
  });

  it("records an approval of one named alternative and never claims execution", async () => {
    mockApi({ ...base, "GET /registry/me": me("reviewer"), [RUN]: () => jsonResponse(run()), "POST /resilience/runs/RO-AAAA1111/decision": () => jsonResponse(run()) });
    renderApp("/resilience/runs/RO-AAAA1111");
    const approve = await screen.findByRole("button", { name: "Approve alternative" });
    expect(approve).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Rationale/), "Reviewed against stress tests.");
    expect(approve).toBeEnabled();
    await userEvent.click(approve);
    expect(await screen.findByText(/approved C00002. No order was placed/)).toBeInTheDocument();
    expect(JSON.parse(calls.find((c) => c.path.endsWith("/decision"))!.text!)).toEqual({ decision: "APPROVED", rationale: "Reviewed against stress tests.", alternative_id: "C00002" });
  });

  it("shows the server's refusal of a self-approval", async () => {
    mockApi({ ...base, "GET /registry/me": me("reviewer"), [RUN]: () => jsonResponse(run()), "POST /resilience/runs/RO-AAAA1111/decision": () => jsonResponse({ error: { code: "conflict", message: "The person who requested a run cannot approve it." } }, 409) });
    renderApp("/resilience/runs/RO-AAAA1111");
    await userEvent.type(await screen.findByLabelText(/Rationale/), "Reviewed against stress tests.");
    await userEvent.click(screen.getByRole("button", { name: "Approve alternative" }));
    expect(await screen.findByText(/who requested a run cannot approve it/)).toBeInTheDocument();
  });

  it("replays from stored inputs and reports whether live inputs changed", async () => {
    mockApi({ ...base, [RUN]: () => jsonResponse(run()), "POST /resilience/runs/RO-AAAA1111/replay": () => jsonResponse({ reproduced_from_stored_snapshot: true, live_inputs_unchanged: false }) });
    renderApp("/resilience/runs/RO-AAAA1111");
    await userEvent.click(await screen.findByRole("button", { name: "Replay from stored inputs" }));
    expect(await screen.findByText(/reproduced the stored result; live inputs have changed/)).toBeInTheDocument();
  });
});

describe("history and limits", () => {
  it("lists runs, filters by result and shows an empty state", async () => {
    mockApi({ ...base, "GET /resilience/runs": (u) => jsonResponse(u.searchParams.get("result_status") === "timed_out" ? runs() : runs(run(), run({ run_uid: "RO-BBBB2222", result_status: "infeasible", alternative_count: 0, front_size: null }))) });
    renderApp("/resilience/runs");
    expect(await screen.findByRole("link", { name: "RO-AAAA1111" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "RO-BBBB2222" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Result"), "timed_out");
    expect(await screen.findByText("No resilience runs yet")).toBeInTheDocument();
    expect(calls.at(-1)!.path).toBe("/resilience/runs");
  });

  it("states that the solver is an approximate grid and lists what is not supported", async () => {
    mockApi({ ...base });
    renderApp("/resilience/about");
    expect(await screen.findByText(/not a proof of global optimality/)).toBeInTheDocument();
    expect(screen.getByText(/per-organisation isolation/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Objective definitions" })).toBeInTheDocument();
    expect(screen.getByText(/soft/)).toBeInTheDocument();
  });
});
