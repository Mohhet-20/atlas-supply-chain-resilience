import { render, screen } from "@testing-library/react";
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

const metric = (m: number | null) => ({ mean: m, p05: m === null ? null : m * 0.9, p95: m === null ? null : m * 1.1 });
const sim = () => ({
  fill_rate: metric(0.97), cycle_service_level: metric(0.95), stockout_days: metric(2), stockout_events: metric(1), units_short: metric(12), avg_on_hand: metric(80), orders: metric(6), turnover: metric(5),
  costs: { holding: null, ordering: null, shortage: null, known_subtotal: null, purchase_spend: null, omitted_components: ["holding", "ordering", "shortage"] },
});
const policy = { type: "S_UPTO", reorder_point: 55, order_quantity: null, order_up_to: 120, review_period_days: 1, min_order_quantity: null, order_increment: null, max_order_quantity: null };
const detail = (over: Record<string, unknown> = {}) => ({
  run_uid: "IR-AAAA1111", kind: "OPTIMIZE", status: "COMPLETED", result_status: "recommended", requested_by: "other", product_id: 1, facility_id: 2, scenario_id: null, forecast_run_id: 9,
  policy_type: "S_UPTO", service_type: "CYCLE", service_target: 0.95, objective: "SERVICE_TARGET", algorithm_version: "io-1", snapshot_hash: "a".repeat(64), scenario_hash: null,
  recommendation_hash: "b".repeat(64), queued_at: "2026-11-01T06:00:00Z", started_at: null, finished_at: null, duration_ms: 12, error: null, order_now: true, order_quantity: 70, order_status: "recommended",
  unit: "kg", hypothetical: false, approval: { state: "PROPOSED", decided_by: null, decided_at: null, rationale: null, executed: false }, request: {}, request_hash: "c".repeat(64),
  position: { status: "OK", unit: "kg", available: 40, usable: 40, backordered: 0, on_order_counted: 10, inventory_position: 50, stale: false, age_days: 1, formula: "position = available - backordered + dated receipts counted", receipts_counted: [], receipts_excluded: [{ id: 3, reason: "OVERDUE", quantity: 25 }], backordered_note: null },
  demand: { mean: 10, sd: 3, source: "FORECAST", sd_source: "OBSERVED", intermittent: false, observed_mean: 9.5, unit: "kg", coverage_ratio: 1, forecast: { forecast_run_id: 9, model: "ets", model_version: "1.2" }, statement: "Demand comes from forecast run 9." },
  lead_time: { days: 6, std: 1, source: "MEASURED", provenance: "MEASURED", n: 12, alternatives: [{ source: "SOURCING_OFFER", days: 8, std: null }] },
  safety_stock: { value: 22, method: "NORMAL_LT", service_type: "CYCLE", service_target: 0.95, protection_days: 7, reorder_level: 55, current: 15, calculated: { assumptions: ["Demand is independent across days."], warnings: [] } },
  recommendation: { status: "recommended", order_now: true, quantity: 70, flags: ["IN_TRANSIT_NOT_ADDED"], inventory_position: 50, trigger: true, rule: "position <= s", arrival_after_days: 6, arrival_date: null, unit: "kg", purchase_spend: 700, currency: "USD", supplier_id: null, decision_support_only: true, statement: "Decision support only. No purchase order is created.", policy, safety_stock: 22, service_target: { type: "CYCLE", target: 0.95 } },
  projection: { horizon_days: 3, demand_basis: "forecast", series: [40, 30, 20, 10], stockout_within_day: null, min_closing: 10, unfilled_demand: 0, days_until_reorder_point: 1, stockout_risk_before_next_arrival: { value: null, basis: null }, note: "Expected-value projection." },
  evaluation: {
    mode: "monte_carlo", synthetic: false, statement: "Simulated, not guaranteed.", assumptions: ["Daily review."], selected: "C1", selection_rule: "Lowest average on hand meeting target.", analytic_id: "x", seed: 7,
    budget: { replications: 50, days: 90, candidates: 2, max_sim_steps: 1000000, note: null },
    candidates: [{ id: "C1", ss_multiplier: 1, q_multiplier: 1, safety_stock: 22, policy, flags: [], meets_service_target: true, metrics: sim() }],
    recommended: { candidate: "C1", metrics: sim(), sample_path: { on_hand: [50, 40, 30], inventory_position: [60, 50, 40], truncated: false }, meets_service_target: true },
    current: null, comparison: null, backtest: { status: "skipped", reason: "needs at least 56 daily observations", train_days: 0, test_days: 0, leakage_guard: "", limitations: [] },
  },
  scenario_results: null, mitigations: null, infeasibility: null,
  costs: { currency: "USD", inputs: {}, provenance: {}, missing_inputs: ["holding_cost_per_unit_day"], statement: "Known subtotal only; unknown components are omitted, not zero.", recommended_policy: null, current_policy: null, order_purchase_spend: 700 },
  explanation: { steps: [{ step: "1", text: "Safety stock covers lead-time variability." }], sensitivity: null, order_quantity: { method: "LEAD_TIME_COVER", reason: "Costs unknown." }, constraints: null },
  warnings: { warnings: [{ code: "SYNTHETIC", severity: "WARN", message: "Inputs are synthetic." }], data_quality: [] },
  provenance: null, reproducibility: {}, statements: ["ATLAS never places purchase orders."], ...over,
});
const listItem = (over: Record<string, unknown> = {}) => ({ ...detail(over), safety_stock: 22 });
const page = (...r: unknown[]) => ({ items: r, total: r.length, limit: 15, offset: 0 });

describe("inventory intelligence center", () => {
  it("shows positions with unknown values as unknown, never zero", async () => {
    mockApi({
      ...base,
      "GET /replenishment/positions": () => jsonResponse({
        items: [{ product_id: 1, facility_id: 2, product_code: "P1", product_name: "Resin A", facility_code: "F2", facility_name: "Plant 2", unit: "kg", available: null, usable: null, backordered: null, observed_at: null, age_days: null, stale: false, inventory_status: "MISSING", demand_per_day: null, demand_status: null, days_of_cover: null, has_policy: false, reorder_point: null, status: "UNKNOWN", flags: ["NO_INVENTORY_OBSERVATION"] }],
        total: 1, limit: 15, offset: 0, capped: false, considered: 1, gaps_on_page: {}, statement: "Synthetic overview.",
      }),
    });
    renderApp("/replenishment");
    expect(await screen.findByText("Resin A")).toBeInTheDocument();
    expect(screen.getAllByText("unknown").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("none registered")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Plan Resin A/ })).toHaveAttribute("href", expect.stringContaining("/replenishment/plan?product=1&facility=2"));
  });

  it("shows an empty state without inventing stock", async () => {
    mockApi({ ...base, "GET /replenishment/positions": () => jsonResponse({ items: [], total: 0, limit: 15, offset: 0, capped: false, considered: 0, gaps_on_page: {}, statement: "x" }) });
    renderApp("/replenishment");
    expect(await screen.findByText(/No inventory observations/)).toBeInTheDocument();
  });

  it("calculates safety stock and the reorder decision without saving", async () => {
    mockApi({
      ...base,
      "POST /replenishment/calc/safety-stock": () => jsonResponse({ result_status: "recommended", safety_stock: detail().safety_stock, demand: detail().demand, lead_time: detail().lead_time, order_quantity: null, infeasibility: [], warnings: [], data_quality: [], sensitivity: null, snapshot_hash: "a" }),
      "POST /replenishment/calc/reorder-point": () => jsonResponse({ result_status: "recommended", recommendation: detail().recommendation, safety_stock: detail().safety_stock, position: detail().position, projection: null, infeasibility: [], explanation: [{ step: "1", text: "Position is below s." }], warnings: [], data_quality: [], label: "Calculation only." }),
    });
    renderApp("/replenishment/plan?product=1&facility=2");
    expect(await screen.findByLabelText(/Product \(registry id\)/)).toHaveValue("1");
    await userEvent.click(screen.getByRole("button", { name: "Calculate now" }));
    expect(await screen.findByText(/Order 70 kg now/)).toBeInTheDocument();
    expect(screen.getByText(/Position is below s/)).toBeInTheDocument();
    const c = calls.find((x) => x.path === "/replenishment/calc/safety-stock");
    expect(JSON.parse(c?.text ?? "{}")).toMatchObject({ product_id: 1, facility_id: 2, service_type: "CYCLE" });
    expect(JSON.parse(c?.text ?? "{}").costs).toBeUndefined();
  });

  it("sends an idempotency key when running and opens the run", async () => {
    mockApi({
      ...base,
      "POST /replenishment/optimize": () => jsonResponse(listItem({ status: "QUEUED" }), 202),
      "GET /replenishment/runs/IR-AAAA1111": () => jsonResponse(detail()),
    });
    renderApp("/replenishment/plan?product=1&facility=2");
    await userEvent.click(await screen.findByRole("button", { name: "Run optimisation" }));
    expect(await screen.findByText(/Order 70 kg now/)).toBeInTheDocument();
    const post = calls.find((x) => x.method === "POST" && x.path === "/replenishment/optimize");
    expect(JSON.stringify(post?.headers)).toMatch(/Idempotency-Key/i);
  });

  it("disables running for viewers", async () => {
    mockApi({ ...base, "GET /registry/me": me("viewer") });
    renderApp("/replenishment/plan");
    expect(await screen.findByRole("button", { name: "Run optimisation" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Simulate my policy" })).toBeDisabled();
  });

  it("shows a run with provenance, assumptions, omitted costs and no purchase order", async () => {
    mockApi({ ...base, "GET /replenishment/runs/IR-AAAA1111": () => jsonResponse(detail()) });
    renderApp("/replenishment/runs/IR-AAAA1111");
    expect(await screen.findByText(/Order 70 kg now/)).toBeInTheDocument();
    expect(screen.getByText(/Forecast version: run 9, model ets 1.2/)).toBeInTheDocument();
    expect(screen.getByText(/Other lead-time sources considered/)).toBeInTheDocument();
    expect(screen.getByText(/1 receipt\(s\) not counted/)).toBeInTheDocument();
    expect(screen.getByText(/Known subtotal only/)).toBeInTheDocument();
    expect(screen.getByText(/Cost inputs not available: holding cost per unit day/i)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Projected closing inventory/ })).toBeInTheDocument();
    expect(screen.getByText(/Not run: needs at least 56 daily observations/)).toBeInTheDocument();
    expect(screen.getByText(/PROPOSED — not approved/)).toBeInTheDocument();
  });

  it("explains an incomplete run instead of recommending", async () => {
    mockApi({ ...base, "GET /replenishment/runs/IR-AAAA1111": () => jsonResponse(detail({ result_status: "incomplete", recommendation: null, evaluation: null, projection: null, infeasibility: [{ code: "NO_DEMAND", message: "No demand history or forecast is registered." }], approval: null })) });
    renderApp("/replenishment/runs/IR-AAAA1111");
    expect(await screen.findByText(/Missing data was not replaced by zero/)).toBeInTheDocument();
    expect(screen.getByText(/No demand history or forecast is registered/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve plan" })).toBeNull();
  });

  it("hides cost figures from viewers", async () => {
    mockApi({ ...base, "GET /registry/me": me("viewer"), "GET /replenishment/runs/IR-AAAA1111": () => jsonResponse(detail({ cost_redacted: true })) });
    renderApp("/replenishment/runs/IR-AAAA1111");
    expect(await screen.findByText(/Cost figures are hidden for your role/)).toBeInTheDocument();
    expect(screen.getByText("hidden for your role")).toBeInTheDocument();
  });

  it("lets a reviewer approve only with a rationale and says no order is placed", async () => {
    mockApi({
      ...base, "GET /registry/me": me("reviewer"),
      "GET /replenishment/runs/IR-AAAA1111": () => jsonResponse(detail()),
      "POST /replenishment/runs/IR-AAAA1111/decision": () => jsonResponse({}),
    });
    renderApp("/replenishment/runs/IR-AAAA1111");
    const approve = await screen.findByRole("button", { name: "Approve plan" });
    expect(approve).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Rationale/), "Reviewed against forecast and stock count.");
    await userEvent.click(approve);
    expect(await screen.findByText(/No purchase order was created/)).toBeInTheDocument();
    const d = calls.find((x) => x.path.endsWith("/decision"));
    expect(JSON.parse(d?.text ?? "{}")).toMatchObject({ decision: "APPROVED" });
  });

  it("lists runs and the capabilities page", async () => {
    mockApi({
      ...base,
      "GET /replenishment/runs": () => jsonResponse(page(listItem())),
      "GET /replenishment/health": () => jsonResponse({ algorithm_version: "io-1", write_api_enabled: true, cost_profiles: 0, runs_by_status: {}, limits: { max_active_runs: 4 }, algorithms: ["EOQ"], not_supported: ["Multi-echelon optimisation"], statements: { a: "ATLAS never places purchase orders." } }),
    });
    const { unmount } = renderApp("/replenishment/runs");
    expect(await screen.findByText("IR-AAAA1111")).toBeInTheDocument();
    unmount();
    renderApp("/replenishment/about");
    expect(await screen.findByText(/Multi-echelon optimisation/)).toBeInTheDocument();
  });
});
