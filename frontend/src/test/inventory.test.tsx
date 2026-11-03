import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { Analysis, Comparison, Finding, FindingDetail, SummaryResponse } from "../lib/inventoryTypes";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; search: URLSearchParams; text?: string; contentType?: string }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const headers = (init?.headers ?? {}) as Record<string, string>;
    calls.push({ method, path, search: url.searchParams, text: typeof init?.body === "string" ? init.body : init?.body ? "<file>" : undefined, contentType: headers["Content-Type"] });
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
const page = <T,>(items: T[], total = items.length) => ({ items, total, limit: 25, offset: 0 });

const analysis = (over: Partial<Analysis> = {}): Analysis => ({
  id: 4, analysis_uid: "SA-4", kind: "BASELINE", status: "COMPLETED", requested_by: "tester", impact_analysis_id: null,
  config: { horizon_days: 60, demand_window_days: 30, max_inventory_age_days: 7, low_coverage_days: 7, include_planned_receipts: false, allocated_overlap: "UNKNOWN" },
  config_hash: "abc", algorithm_version: "inv-1", as_of: "2026-10-27T10:00:00Z", input_hash: "0123456789abcdef", queued_at: "2026-10-27T10:00:00Z", started_at: null, finished_at: null,
  duration_ms: 5, truncated: false, truncation_reasons: [], error: null, disclaimer: "d",
  summary: { pairs_analysed: 3, by_status: { projected_stockout: 1, unknown: 1, insufficient_data: 1 }, kind: "BASELINE", note: "Unknown and insufficient-data pairs are counted separately." },
  ...over,
});
const finding = (over: Partial<Finding> = {}): Finding => ({
  id: 11, analysis_id: 4, product_id: 1, facility_id: 2, product_code: "P-1", product_name: "Elm Widget", facility_code: "F-1", facility_name: "Cedar Plant", unit: "EA",
  status: "projected_stockout", severity_rank: 3, reason: "r", change: null, confidence: "MEDIUM", coverage: 0.4, days_of_inventory: 10, days_of_inventory_status: "calculable",
  stockout_within_day: 30, explanation: "Under the stated assumptions inventory reaches zero.", ...over,
});
const detail = (f: Finding, over: Partial<FindingDetail> = {}): FindingDetail => ({
  ...f, quantities: { usable: 100, available: 100, basis_value: 100, observed_at: "2026-10-26T10:00:00Z" }, demand: { rate_per_day: 10, status: "calculable" },
  coverage_days: { status: "calculable", days: 10 }, policy: { has_policy: false, thresholds: {}, violations: [], missing: ["safety_stock"] },
  projection: { series: [100, 90, 80, 70, 0], receipts_by_day: {}, min_closing: 0, stockout_within_day: 4, unfilled_demand: 5, unfilled_demand_note: "Not a forecast of lost sales.", formula: "closing(d) = max(0, closing(d-1) + receipts(d) - demand_rate)", opening: 100, rate_per_day: 10, horizon_days: 4, as_of: "2026-10-27T10:00:00Z" },
  scenario: null, confidence_basis: ["no policy"], coverage_detail: {}, data_quality: [{ code: "NO_POLICY", severity: "INFO", message: "No inventory policy applies." }],
  assumptions: ["Demand continues at the observed rate."], evidence: { position_id: 1 }, disclaimer: "d", ...over,
});
const sumResp = (a: Analysis): SummaryResponse => ({ analysis: a, summary: a.summary!, warnings: [], failures: [], inputs: {}, disclaimer: "d" });
const analysisRoutes = (a: Analysis, items: Finding[]): Record<string, Handler> => ({
  "GET /registry/me": me("viewer"),
  [`GET /inventory/analyses/${a.id}/summary`]: () => jsonResponse(sumResp(a)),
  [`GET /inventory/analyses/${a.id}/findings`]: () => jsonResponse(page(items)),
  [`GET /inventory/analyses/${a.id}/findings/11`]: () => jsonResponse(detail(finding())),
  [`GET /inventory/analyses/${a.id}/findings/12`]: () => jsonResponse(detail(finding({ id: 12, status: "unknown", stockout_within_day: null, days_of_inventory: null, days_of_inventory_status: "insufficient_demand_history" }), { projection: null })),
});

describe("overview", () => {
  it("explains that nothing is shown until data is imported", async () => {
    mockApi({
      "GET /registry/me": me("viewer"),
      "GET /inventory/quality": () => jsonResponse({ pairs_with_inventory: 0, stale_pairs: 0, max_age_days: 7, pairs_without_demand: 0, pairs_without_policy: 0, import_rows_by_outcome: {}, top_rejection_codes: [], note: "n" }),
      "GET /inventory/analyses": () => jsonResponse(page([])),
    });
    renderApp("/inventory");
    expect(await screen.findByText("No inventory data yet")).toBeInTheDocument();
    expect(screen.getByText("No analyses yet")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(/not a forecast/i);
  });

  it("shows an error with retry when the API fails", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /inventory/quality": () => jsonResponse({ error: { code: "boom", message: "Server trouble" } }, 500), "GET /inventory/analyses": () => jsonResponse(page([])) });
    renderApp("/inventory");
    expect(await screen.findByText("Server trouble")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Retry" }).length).toBeGreaterThan(0);
  });
});

describe("explorer", () => {
  it("never shows a missing quantity as zero and labels in-transit as not on hand", async () => {
    mockApi({
      "GET /registry/me": me("viewer"),
      "GET /inventory/positions": () => jsonResponse({ ...page([{ id: 1, product_id: 1, facility_id: 2, product_name: "Elm Widget", facility_name: "Cedar Plant", observed_at: "2026-10-26T10:00:00Z", unit: "EA", on_hand: 100, reserved: null, allocated: null, quarantined: null, damaged: null, usable_source: null, atp_source: null, in_transit: 40, backordered: null, provenance: "USER_PROVIDED", source_label: "wms", batch_id: 1 }]), latest_only: true }),
    });
    renderApp("/inventory/positions");
    const row = (await screen.findByText("Elm Widget")).closest("tr")!;
    const cells = within(row).getAllByRole("cell").map((c) => c.textContent);
    expect(cells).toContain("—");
    expect(cells).not.toContain("0");
    expect(screen.getByText(/In transit \(not on hand\)/)).toBeInTheDocument();
    expect(cells).toContain("2026-10-26 10:00 UTC");
  });
});

describe("analyses list", () => {
  it("disables starting for viewers and posts a scenario request for editors", async () => {
    const spy = mockApi({
      "GET /registry/me": me("viewer"),
      "GET /inventory/analyses": () => jsonResponse(page([analysis()])),
    });
    const { unmount } = renderApp("/inventory/analyses");
    expect(await screen.findByRole("button", { name: "Run baseline analysis" })).toBeDisabled();
    unmount();
    spy.mockRestore();
    mockApi({
      "GET /registry/me": me("editor"),
      "GET /inventory/analyses": () => jsonResponse(page([analysis()])),
      "POST /inventory/analyses": () => jsonResponse(analysis({ id: 9, kind: "SCENARIO", impact_analysis_id: 7, status: "QUEUED", summary: null }), 202),
      "GET /inventory/analyses/9/summary": () => jsonResponse({ error: { code: "conflict", message: "Analysis is QUEUED; results are available when it finishes." } }, 409),
      "GET /inventory/analyses/9/findings": () => jsonResponse({ error: { code: "conflict", message: "Analysis is QUEUED" } }, 409),
    });
    renderApp("/inventory/analyses");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/Cascading impact analysis number/), "7");
    await user.click(screen.getByRole("button", { name: "Run scenario analysis" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/inventory/analyses")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(JSON.parse(post.text!)).toEqual({ impact_analysis_id: 7, config: { horizon_days: 60 } });
  });
});

describe("analysis page", () => {
  it("separates unknown from shortages and labels the chart observed vs projected", async () => {
    const a = analysis();
    mockApi(analysisRoutes(a, [finding(), finding({ id: 12, product_name: "Fir Bracket", status: "unknown", stockout_within_day: null, days_of_inventory: null, days_of_inventory_status: "insufficient_demand_history", severity_rank: 0 })]));
    renderApp("/inventory/analyses/4");
    expect(await screen.findByText("Elm Widget")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Show details for Elm Widget/ }));
    const img = await screen.findByRole("img", { name: /observed 100 .* then projected over 4 days.*conditional/i });
    expect(img).toBeInTheDocument();
    expect(screen.getByText(/Observed \(snapshot, day 0\)/)).toBeInTheDocument();
    expect(screen.getByText(/Projected baseline \(conditional\)/)).toBeInTheDocument();
    expect(screen.getByText(/reaches zero in day 4/)).toBeInTheDocument();
    expect(screen.getByText(/Not supplied by the policy \(unknown, not zero\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Show details for Fir Bracket/ }));
    expect(await screen.findByText(/This is unknown, not safe/)).toBeInTheDocument();
  });

  it("flags an incomplete result", async () => {
    const a = analysis({ status: "PARTIAL", truncated: true, truncation_reasons: ["max_pairs"] });
    mockApi(analysisRoutes(a, [finding()]));
    renderApp("/inventory/analyses/4");
    expect(await screen.findByRole("alert")).toHaveTextContent(/incomplete \(max_pairs\)/);
  });

  it("shows scenario bounds as a bound, not a prediction", async () => {
    const a = analysis({ kind: "SCENARIO", impact_analysis_id: 3 });
    const f = finding({ change: "CONDITIONAL_EARLIER_CROSSING" });
    const routes = analysisRoutes(a, [f]);
    routes["GET /inventory/analyses/4/findings/11"] = () =>
      jsonResponse(detail(f, { scenario: { impact_analysis_id: 3, facility_exposed: true, outcome: "CONDITIONAL_BOUNDS", effects: [], series_worst: [100, 50, 0, 0, 0], series_best: [100, 90, 80, 70, 60], stockout_within_day_worst: 2, note: "Range from no loss to total loss." } }));
    mockApi(routes);
    renderApp("/inventory/analyses/4");
    await userEvent.setup().click(await screen.findByRole("button", { name: /Show details for Elm Widget/ }));
    expect(await screen.findByText(/This is a bound, not a prediction/)).toBeInTheDocument();
    expect(screen.getByText(/Scenario worst-case bound/)).toBeInTheDocument();
  });
});

describe("scenario comparison", () => {
  const cmp = (over: Partial<Comparison> = {}): Comparison => ({
    baseline_analysis_id: 4, scenario_analysis_id: 5, comparable: true, caveats: [], pairs: 3, changed: 1, not_assessable: 1,
    items: [{ product_id: 1, facility_id: 2, product_name: "Elm Widget", facility_name: "Cedar Plant", baseline_status: "projected_stockout", scenario_status: "projected_stockout", change: "EARLIER_CROSSING", baseline_stockout_day: 30, scenario_stockout_day: 10, scenario_stockout_day_worst: null, scenario_outcome: "QUANTIFIED", scenario_finding_id: 2, baseline_finding_id: 1 }],
    total: 1, limit: 50, offset: 0, impact_analysis_id: 3, note: "Scenario results are hypothetical and conditional.", ...over,
  });
  it("shows the change and counts non-assessable pairs separately", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /inventory/analyses/compare": () => jsonResponse(cmp()) });
    renderApp("/inventory/compare?baseline=4&scenario=5");
    expect(await screen.findByText("Elm Widget")).toBeInTheDocument();
    expect(screen.getByText(/1 could not be assessed/)).toBeInTheDocument();
    expect(screen.getByText("earlier crossing")).toBeInTheDocument();
  });
  it("warns when the analyses are not comparable", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /inventory/analyses/compare": () => jsonResponse(cmp({ comparable: false, caveats: [{ code: "CONFIG_DIFFERS", text: "The two analyses used different settings." }] })) });
    renderApp("/inventory/compare?baseline=4&scenario=5");
    expect(await screen.findByRole("alert")).toHaveTextContent(/not directly comparable/);
    expect(screen.getByText(/different settings/)).toBeInTheDocument();
  });
  it("asks for both analyses when none is chosen", async () => {
    mockApi({ "GET /registry/me": me("viewer") });
    renderApp("/inventory/compare");
    expect(await screen.findByText("Choose a baseline and a scenario analysis")).toBeInTheDocument();
  });
});

describe("import wizard", () => {
  const limits = {
    max_upload_bytes: 5_000_000, max_import_rows: 10000, max_pairs: 1000, max_horizon_days: 365, time_budget_seconds: 30, max_active_analyses: 3, date_formats: ["%Y-%m-%d"],
    fields: { INVENTORY: { required: ["product_code", "facility_code", "observed_at", "unit", "on_hand"], optional: ["reserved"] }, DEMAND: { required: [], optional: [] }, REPLENISHMENT: { required: [], optional: [] } },
    demand_kinds: [], receipt_statuses: [], algorithm_version: "inv-1", disclaimer: "d",
  };
  const prev = {
    mode: "preview", committed: false, would_write: 1,
    summary: { kind: "INVENTORY", rows_total: 2, rows_accepted: 1, rows_duplicate: 0, rows_rejected: 1, error_codes: { NEGATIVE_QUANTITY: 1 }, warning_codes: {}, columns: ["product_code", "facility_code", "observed_at", "unit", "on_hand"], mapping: { product_code: "product_code", on_hand: "on_hand" } },
    rows: [{ row_number: 3, outcome: "REJECTED", errors: [{ code: "NEGATIVE_QUANTITY", field: "on_hand", message: "on_hand cannot be negative." }], warnings: [], raw: { on_hand: "-5" } }],
    sample_accepted: [{ row_number: 2, outcome: "ACCEPTED", errors: [], warnings: [], raw: { product_code: "P-1", on_hand: "10" } }],
  };
  const batch = { id: 8, batch_uid: "IB-8", kind: "INVENTORY", status: "COMPLETED", source_label: "wms", provenance: "USER_PROVIDED", file_name: "inv.csv", file_format: "CSV", requested_by: "tester", mapping: null, rows_total: 2, rows_accepted: 1, rows_duplicate: 0, rows_rejected: 1, summary: null, created_at: "2026-10-27T10:00:00Z" };

  it("walks file -> mapping -> preview -> validation -> confirm -> status -> errors", async () => {
    mockApi({
      "GET /registry/me": me("editor"),
      "GET /inventory/limits": () => jsonResponse(limits),
      "POST /inventory/imports": (url) => (url.searchParams.get("mode") === "commit" ? jsonResponse({ mode: "commit", committed: true, created: true, replayed_existing_batch: false, batch }) : jsonResponse(prev)),
      "GET /inventory/imports/8/rows": () => jsonResponse(page([{ row_number: 3, outcome: "REJECTED", errors: [{ code: "NEGATIVE_QUANTITY", field: "on_hand", message: "on_hand cannot be negative." }], raw: { on_hand: "-5" }, record_id: null }])),
    });
    renderApp("/inventory/import");
    const user = userEvent.setup();
    const file = new File(["product_code,on_hand\nP-1,10\n"], "inv.csv", { type: "text/csv" });
    await user.upload(await screen.findByLabelText(/File \(.csv or .json\)/), file);
    await user.click(screen.getByRole("button", { name: "Read file" }));
    expect(await screen.findByLabelText("Map columns")).toBeInTheDocument();
    const first = calls.find((c) => c.method === "POST")!;
    expect(first.contentType).toBe("text/csv");
    await user.click(screen.getByRole("button", { name: "Preview with this mapping" }));
    expect(await screen.findByText(/Nothing has been written/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Review validation" }));
    expect(await screen.findByText(/on_hand cannot be negative/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Confirm and import" }));
    expect(await screen.findByRole("status")).toHaveTextContent(/1 added, 0 duplicates, 1 rejected of 2/);
    const commit = calls.filter((c) => c.method === "POST").pop()!;
    expect(commit.search.get("mode")).toBe("commit");
    expect(commit.search.get("kind")).toBe("INVENTORY");
    await user.click(screen.getByRole("button", { name: "View row errors" }));
    expect(await screen.findByText("NEGATIVE_QUANTITY")).toBeInTheDocument();
  });

  it("does not allow reading a file for a viewer", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /inventory/limits": () => jsonResponse(limits) });
    renderApp("/inventory/import");
    await user_upload();
    expect(screen.getByRole("button", { name: "Read file" })).toBeDisabled();
  });
  async function user_upload() {
    const user = userEvent.setup();
    await user.upload(await screen.findByLabelText(/File \(.csv or .json\)/), new File(["a"], "a.csv", { type: "text/csv" }));
  }
});

describe("policies", () => {
  it("explains that a missing threshold is unknown, not zero", async () => {
    mockApi({
      "GET /registry/me": me("viewer"),
      "GET /inventory/policies": () => jsonResponse(page([])),
      "GET /inventory/policies/missing": () => jsonResponse({ ...page([{ product_id: 1, facility_id: 2, product_name: "Elm Widget", facility_name: "Cedar Plant" }]), limit: 10 }),
    });
    renderApp("/inventory/policies");
    expect(await screen.findByText(/have inventory but no policy/)).toHaveTextContent(/Elm Widget at Cedar Plant/);
    expect(screen.getByText("No policies recorded")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(/unknown, not zero/);
  });
});
