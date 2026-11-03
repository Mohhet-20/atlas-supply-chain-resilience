import { render, screen, within } from "@testing-library/react";
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
const base = { "GET /registry/me": me("editor"), "GET /scenarios": () => jsonResponse({ items: [{ id: 7, name: "Alpha outage" }], total: 1, limit: 100, offset: 0 }) };

const comp = (metric: string, raw: number | null, w = 0.2) => ({ metric, raw, normalised: raw === null ? null : 0.5, weight: w, contribution: raw === null ? 0 : 10, direction: "lower", available: raw !== null });
const cand = (over: Record<string, unknown> = {}) => ({
  offer_uid: "OF-1", supplier_id: 1, supplier_code: "S1", supplier_name: "Alpha Resins", country: "Testland", status: "ELIGIBLE", reasons: [], missing: [], evidence: [{ rule: "approval", outcome: "PASS", detail: "approved" }],
  rank: 1, score: { score: 70, components: [comp("price", 10), comp("reliability", null)], missing_metrics: ["reliability"], complete: false, weight_coverage: 0.8 }, objective_excluded: null,
  allocated_quantity: 0, terms: { unit: "kg", unit_price: 10, currency: "USD", freight_per_unit: null, capacity: 40, effective_capacity: 40, min_order_quantity: null, lot_size: null, max_allocation: null, lead_time_days: 10, defect_rate: 0.01, approval: "APPROVED", relationship: "CONFIRMED", offer_status: "ACTIVE", observed_at: null, provenance: "SYNTHETIC_DEMO", quality: {} },
  risk: null, reliability: { value: null, n: 0, interval: null, note: "0 usable deliveries (minimum 20); not used as a measurement" }, transport: null, scenario_notes: [], ...over,
});
const alt = (over: Record<string, unknown> = {}) => ({
  counts: { ELIGIBLE: 1, MISSING_INFORMATION: 1 }, price_basis: "PURCHASE_ONLY", ranking_incomplete: true, warnings: [], statements: [],
  candidates: [cand(), cand({ offer_uid: null, supplier_id: 2, supplier_name: "Link Only Co", status: "MISSING_INFORMATION", rank: null, score: null, terms: null, missing: ["commercial offer (price, capacity, lead time)"] })], ...over,
});
const metrics = (over: Record<string, unknown> = {}) => ({
  currency: "USD", allocated_quantity: 60, demand: 60, unmet_quantity: 0, fulfilment_pct: 100, purchase_cost: 640, freight_cost: null, landed_cost_stated: null, cost_basis: "purchase only",
  average_unit_cost: 10.67, average_lead_time_days: 10, slowest_lead_time_days: 10, weighted_risk: null, supplier_count: 2, hhi: 0.556, effective_suppliers: 1.8, max_supplier_share: 0.667,
  country_hhi: 1, country_shares: {}, concentration_note: "x", ...over,
});
const alloc = (name: string, q: number, price: number | null) => ({ offer_uid: `OF-${name}`, supplier_id: 1, supplier_code: name, supplier_name: name, country: null, quantity: q, unit: "kg", unit_price: price, freight_per_unit: null, purchase_cost: price === null ? null : price * q, lead_time_days: 10, capacity: 40, capacity_utilisation: 1, suitability_score: 70, rank: 1, share: q / 60 });
const run = (over: Record<string, unknown> = {}) => ({
  run_uid: "SR-AAAA1111", kind: "OPTIMIZE", status: "COMPLETED", result_status: "feasible", requested_by: "other", product_id: 1, destination_facility_id: 2, scenario_id: null, objective: "LOWEST_COST", strategy: "AUTO",
  demand: 60, unit: "kg", algorithm_version: "sc-1", snapshot_hash: "d".repeat(64), queued_at: "2026-11-01T06:00:00Z", finished_at: null, duration_ms: 5, error: null, supplier_count: 2, unmet_quantity: 0, hhi: 0.556,
  hypothetical: false, approval: { state: "PROPOSED", decided_by: null, decided_at: null, rationale: null, executed: false }, purchase_cost: 640, request: {}, request_hash: "e".repeat(64),
  candidates: [cand({ allocated_quantity: 40 })], allocations: [alloc("Alpha Resins", 40, 10), alloc("Beta Polymers", 20, 12)], baseline_allocations: null, comparison: null, infeasibility: null, metrics: metrics(), disruptions: null,
  summary: { hard_constraint_checks: [{ check: "demand", passed: true, detail: "60 = 60" }], solver: { optimality: "EXACT", method: "bnb", status: "OPTIMAL" } }, warnings: ["Offers are synthetic demonstration data."], statements: ["A plan is a proposal."], reproducibility: {}, ...over,
});
const runs = (...r: unknown[]) => ({ items: r, total: r.length, limit: 15, offset: 0 });

describe("requirement workspace", () => {
  it("previews alternatives with eligibility words, missing information and a visible score breakdown", async () => {
    mockApi({ ...base, "POST /sourcing/alternatives": () => jsonResponse(alt()) });
    renderApp("/sourcing");
    await userEvent.type(await screen.findByLabelText(/Product \(registry id\)/), "1");
    await userEvent.type(screen.getByLabelText(/Destination facility/), "2");
    await userEvent.type(screen.getByLabelText(/Quantity needed/), "60");
    await userEvent.type(screen.getByLabelText(/^Unit/), "kg");
    await userEvent.click(screen.getByRole("button", { name: "Preview alternatives" }));
    expect(await screen.findByRole("heading", { name: /Alternatives \(1 eligible, 1 missing information\)/ })).toBeInTheDocument();
    expect(screen.getByText(/ranking is incomplete/)).toBeInTheDocument();
    expect(screen.getByText("missing information")).toBeInTheDocument();
    expect(screen.getByText("not ranked")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show evidence for Link Only Co" }));
    expect(screen.getByText(/commercial offer/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show evidence for Alpha Resins" }));
    const t = screen.getByRole("table", { name: "Suitability score components" });
    expect(within(t).getByText("missing")).toBeInTheDocument();
    expect(within(t).getByText(/not a probability/)).toBeInTheDocument();
    const body = JSON.parse(calls.find((c) => c.path === "/sourcing/alternatives")!.text!);
    expect(body).toMatchObject({ product_id: 1, destination_facility_id: 2, quantity: 60, unit: "kg", currency: "USD", strategy: "AUTO", fulfillment: "FULL" });
  });

  it("sends constraints, the scenario and an idempotency key when running, then opens the run", async () => {
    mockApi({ ...base, "POST /sourcing/optimize": () => jsonResponse(run({ status: "QUEUED", result_status: null }), 202), "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run()) });
    renderApp("/sourcing");
    await userEvent.type(await screen.findByLabelText(/Product \(registry id\)/), "1");
    await userEvent.type(screen.getByLabelText(/Destination facility/), "2");
    await userEvent.type(screen.getByLabelText(/Quantity needed/), "60");
    await userEvent.type(screen.getByLabelText(/^Unit/), "kg");
    await userEvent.type(screen.getByLabelText(/Max lead time/), "14");
    await userEvent.type(screen.getByLabelText(/Max share per supplier/), "0.6");
    await userEvent.selectOptions(screen.getByLabelText(/^Objective/), "LOWEST_RISK");
    await userEvent.selectOptions(await screen.findByLabelText("Scenario (optional)"), "7");
    await userEvent.click(screen.getByRole("button", { name: "Run optimisation" }));
    expect(await screen.findByRole("heading", { name: "Proposed allocation" })).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST")!;
    expect(JSON.parse(post.text!)).toMatchObject({ objective: "LOWEST_RISK", scenario_id: 7, constraints: { max_lead_time_days: 14, max_supplier_share: 0.6 } });
    expect((post.headers as Record<string, string>)["Idempotency-Key"]).toMatch(/^ui-/);
  });

  it("does not let a viewer run an optimisation and shows the server's rejection", async () => {
    mockApi({ ...base, "GET /registry/me": me("viewer") });
    renderApp("/sourcing");
    await screen.findByLabelText(/Product \(registry id\)/);
    expect(screen.getByRole("button", { name: "Run optimisation" })).toBeDisabled();
    expect(screen.getByText(/needs the editor role/)).toBeInTheDocument();
  });

  it("shows the API's validation message", async () => {
    mockApi({ ...base, "POST /sourcing/alternatives": () => jsonResponse({ error: { code: "validation_error", message: "unit lb does not match the product unit kg" } }, 422) });
    renderApp("/sourcing");
    await userEvent.type(await screen.findByLabelText(/Product \(registry id\)/), "1");
    await userEvent.type(screen.getByLabelText(/Destination facility/), "2");
    await userEvent.type(screen.getByLabelText(/Quantity needed/), "60");
    await userEvent.type(screen.getByLabelText(/^Unit/), "lb");
    await userEvent.click(screen.getByRole("button", { name: "Preview alternatives" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("does not match the product unit");
  });
});

describe("run results", () => {
  it("shows the proposed allocation as PROPOSED with a traceability footer", async () => {
    mockApi({ ...base, "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run()) });
    renderApp("/sourcing/runs/SR-AAAA1111");
    expect(await screen.findByRole("heading", { name: "Proposed allocation" })).toBeInTheDocument();
    expect(screen.getByText(/PROPOSED — not approved/)).toBeInTheDocument();
    expect(screen.getByText(/640 USD/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Allocation by supplier" })).toBeInTheDocument();
    expect(screen.getByText(/Offers are synthetic/)).toBeInTheDocument();
    expect(screen.getByText(/inputs snapshot dddddddddddd/)).toBeInTheDocument();
    await userEvent.click(screen.getByText(/Independent hard-constraint re-check/));
    expect(screen.getByText(/passed — demand/)).toBeInTheDocument();
  });

  it("never calls a partial plan feasible and offers no approval", async () => {
    mockApi({ ...base, "GET /registry/me": me("reviewer"), "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run({ result_status: "partial", metrics: metrics({ allocated_quantity: 50, unmet_quantity: 10, fulfilment_pct: 83.3 }) })) });
    renderApp("/sourcing/runs/SR-AAAA1111");
    expect(await screen.findByRole("heading", { name: "Partial plan (not feasible)" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/NOT a feasible plan/);
    expect(screen.queryByRole("button", { name: "Approve plan" })).not.toBeInTheDocument();
  });

  it("explains infeasibility with reasons and the information that would help", async () => {
    mockApi({ ...base, "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run({ result_status: "infeasible", allocations: [], metrics: null, infeasibility: { reasons: [{ code: "INSUFFICIENT_CAPACITY", detail: "Eligible capacity 50 kg is below demand 60 kg." }], information_that_would_help: ["Capacity from pending suppliers"] } })) });
    renderApp("/sourcing/runs/SR-AAAA1111");
    expect(await screen.findByRole("heading", { name: "Why no full plan exists" })).toBeInTheDocument();
    expect(screen.getByText(/Eligible capacity 50 kg/)).toBeInTheDocument();
    expect(screen.getByText("Capacity from pending suppliers")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Proposed allocation" })).not.toBeInTheDocument();
  });

  it("hides commercial figures from a viewer instead of showing zero", async () => {
    mockApi({ ...base, "GET /registry/me": me("viewer"), "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run({ cost_redacted: true, purchase_cost: null, metrics: metrics({ purchase_cost: null }), allocations: [alloc("Alpha Resins", 40, null)], candidates: [] })) });
    renderApp("/sourcing/runs/SR-AAAA1111");
    await screen.findByRole("heading", { name: "Proposed allocation" });
    expect(screen.getAllByText("hidden for your role").length).toBeGreaterThan(0);
    expect(screen.queryByText(/^0 USD/)).not.toBeInTheDocument();
  });

  it("lets a reviewer record a decision only with a rationale and says no order is placed", async () => {
    mockApi({
      ...base, "GET /registry/me": me("reviewer"), "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run()),
      "POST /sourcing/runs/SR-AAAA1111/decision": () => jsonResponse(run({ approval: { state: "APPROVED", decided_by: "tester", decided_at: "x", rationale: "ok", executed: false } })),
    });
    renderApp("/sourcing/runs/SR-AAAA1111");
    const approve = await screen.findByRole("button", { name: "Approve plan" });
    expect(approve).toBeDisabled();
    expect(screen.getByText(/does not place purchase orders/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Rationale/), "Meets demand within ceilings.");
    await userEvent.click(approve);
    expect(await screen.findByText(/No order was placed/)).toBeInTheDocument();
    expect(JSON.parse(calls.find((c) => c.method === "POST")!.text!)).toEqual({ decision: "APPROVED", rationale: "Meets demand within ceilings." });
  });

  it("offers no approval for a what-if run or to an editor", async () => {
    mockApi({ ...base, "GET /registry/me": me("reviewer"), "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run({ hypothetical: true })) });
    renderApp("/sourcing/runs/SR-AAAA1111");
    await screen.findByRole("heading", { name: "Proposed allocation" });
    expect(screen.getByText(/WHAT-IF/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve plan" })).not.toBeInTheDocument();
  });

  it("shows baseline vs scenario with what could not be applied", async () => {
    const comparison = {
      feasibility_changed: true, status: { baseline: "feasible", scenario: "infeasible" }, purchase_cost: null, hhi: null, unmet_quantity: { baseline: 0, scenario: 60, change: 60 }, average_lead_time_days: null,
      reallocation: [{ offer_uid: "OF-1", supplier_code: "S1", baseline_quantity: 40, scenario_quantity: 0, change: -40 }], suppliers_lost: ["S1"], suppliers_gained: [],
      changed_assumptions: [{ source: "SUPPLIER_UNAVAILABLE", effect: "offers marked unavailable", offers_affected: 1 }], not_applied: [{ event_type: "FACILITY_CAPACITY_REDUCTION", reason: "could not be mapped" }], note: "",
    };
    mockApi({ ...base, "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run({ scenario_id: 7, hypothetical: true, comparison, result_status: "infeasible", allocations: [], metrics: null, infeasibility: { reasons: [] } })) });
    renderApp("/sourcing/runs/SR-AAAA1111");
    expect(await screen.findByRole("heading", { name: "Baseline vs scenario" })).toBeInTheDocument();
    expect(screen.getByText(/Baseline records were not modified/)).toBeInTheDocument();
    expect(screen.getByText(/FACILITY_CAPACITY_REDUCTION \(could not be mapped\)/)).toBeInTheDocument();
  });

  it("shows the strategy comparison table", async () => {
    const strategies = {
      SINGLE: { result_status: "feasible", supplier_count: 1, purchase_cost: 600, hhi: 1, exposure_if_executed_unchanged: { lost_share: 1 }, reoptimised_under_scenario: { result_status: "feasible" } },
      DUAL: { result_status: "feasible", supplier_count: 2, purchase_cost: 640, hhi: 0.55, exposure_if_executed_unchanged: { lost_share: 0.5 }, reoptimised_under_scenario: { result_status: "feasible" } },
    };
    mockApi({ ...base, "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run({ kind: "COMPARE", approval: null, allocations: [], comparison: { strategies }, metrics: null, candidates: [] })) });
    renderApp("/sourcing/runs/SR-AAAA1111");
    const t = await screen.findByRole("table", { name: "Strategy comparison" });
    expect(within(t).getAllByRole("row")).toHaveLength(3);
    expect(within(t).getByText("100%")).toBeInTheDocument();
  });

  it("polls a queued run and offers cancel", async () => {
    mockApi({ ...base, "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run({ status: "QUEUED", result_status: null, allocations: [], metrics: null })) });
    renderApp("/sourcing/runs/SR-AAAA1111");
    expect(await screen.findByText("Queued…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("shows a failed run without inventing a plan", async () => {
    mockApi({ ...base, "GET /sourcing/runs/SR-AAAA1111": () => jsonResponse(run({ status: "FAILED", result_status: null, error: "time budget exceeded", allocations: [] })) });
    renderApp("/sourcing/runs/SR-AAAA1111");
    expect(await screen.findByRole("alert")).toHaveTextContent(/failed: time budget exceeded/);
    expect(screen.queryByRole("heading", { name: "Proposed allocation" })).not.toBeInTheDocument();
  });
});

describe("history, offers and capabilities", () => {
  it("lists runs with their approval state and filters by result", async () => {
    mockApi({ ...base, "GET /sourcing/runs": () => jsonResponse(runs(run(), run({ run_uid: "SR-BBBB2222", result_status: "partial", approval: { state: "PROPOSED", decided_by: null, decided_at: null, rationale: null, executed: false } })) ) });
    renderApp("/sourcing/runs");
    expect(await screen.findByRole("link", { name: "SR-AAAA1111" })).toBeInTheDocument();
    expect(screen.getAllByText(/PROPOSED — not approved/).length).toBe(2);
    await userEvent.selectOptions(screen.getByLabelText("Result"), "partial");
    expect(calls.some((c) => c.path === "/sourcing/runs")).toBe(true);
  });

  it("shows an empty state", async () => {
    mockApi({ ...base, "GET /sourcing/runs": () => jsonResponse(runs()) });
    renderApp("/sourcing/runs");
    expect(await screen.findByText("No sourcing runs yet")).toBeInTheDocument();
  });

  it("shows offers with hidden prices for a viewer and lets an editor suspend one", async () => {
    const offer = { offer_uid: "OF-1", supplier_id: 1, product_id: 1, destination_facility_id: 2, approval_status: "APPROVED", unit: "kg", unit_price: null, price_redacted: true, currency: "USD", price_quality: "VERIFIED", freight_per_unit: null, capacity_amount: 40, capacity_quality: "VERIFIED", min_order_quantity: null, lot_size: null, max_allocation: null, lead_time_days: 10, lead_time_quality: "VERIFIED", defect_rate: null, contract_valid_from: null, contract_valid_to: null, status: "ACTIVE", provenance: "SYNTHETIC_DEMO", observed_at: null, created_by: "x", created_at: "2026-11-01T00:00:00Z" };
    mockApi({ ...base, "GET /sourcing/offers": () => jsonResponse({ items: [offer], total: 1, limit: 15, offset: 0 }), "POST /sourcing/offers/OF-1/status": () => jsonResponse({ ...offer, status: "SUSPENDED" }) });
    renderApp("/sourcing/offers");
    expect(await screen.findByText("hidden for your role")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Suspend OF-1" }));
    expect(await screen.findByText("Offer OF-1 is now suspended.")).toBeInTheDocument();
  });

  it("states what is not supported", async () => {
    mockApi({ ...base, "GET /sourcing/health": () => jsonResponse({ algorithm_version: "sc-1", write_api_enabled: true, offers: 0, runs_by_status: {}, limits: { max_candidates: 40 }, algorithms: ["exact allocation"], not_supported: ["placing purchase orders or changing contracts"], statements: { decision_support: "A sourcing plan is a modelled proposal.", score: "Not a probability." } }) });
    renderApp("/sourcing/about");
    expect(await screen.findByText("placing purchase orders or changing contracts")).toBeInTheDocument();
    expect(screen.getAllByText("A sourcing plan is a modelled proposal.").length).toBeGreaterThan(0);
  });
});
