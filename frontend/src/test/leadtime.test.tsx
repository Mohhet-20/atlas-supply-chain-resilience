import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

// All data below is SYNTHETIC test data.
type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; search: URLSearchParams; text?: string }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/v1/, "");
    calls.push({ method, path, search: url.searchParams, text: typeof init?.body === "string" ? init.body : undefined });
    const h = routes[`${method} ${path}`];
    return Promise.resolve(h ? h(url, init) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
  });
}
const renderApp = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
beforeEach(() => tokenStore.clear());
afterEach(() => vi.restoreAllMocks());

const me = (role: string, writes = true) => () => jsonResponse({ actor: "tester", role, authenticated: true, auth_required: true, writes_enabled: writes });
const page = <T,>(items: T[], total = items.length) => ({ items, total, limit: 25, offset: 0 });
const limits = () => jsonResponse({ algorithm_version: "lt-1", statements: { evidence: "Historical performance is evidence, not a guarantee.", score: "An uncalibrated score is not a probability.", rate: "A historical rate is not the chance this delivery is late." }, tenancy: "Data is separated by organisation." });
const summary = (over: Record<string, unknown> = {}) => ({
  definition: "ORDER_TO_RECEIPT", unit: "CALENDAR_DAYS", description: "Order placement to receipt", as_of: "2026-10-30T00:00:00Z",
  counts: { deliveries: 120, by_state: {}, eligible_completed: 100, with_lead_time: 100, censored: 2, excluded_by_reason: {} },
  lead_time: { n: 100, mean: 10.4, median: 10, std: 3, min: 3, max: 20, percentiles: {}, percentiles_unsupported: {}, note_max: null },
  punctuality: { commitment: "ORIGINAL", grace_hours: 0, n: 90, n_missing_commitment: 10, on_time: 63, late: 27, on_time_rate: 0.7, late_rate: 0.3, late_rate_ci95: [0.21, 0.4], mean_delay_when_late: 2.1, note: "n" },
  freshness: { latest_outcome_at: null, age_days: null }, min_sample: 8, sufficient: true, warnings: ["In-transit and open orders are censored."], statements: [], ...over,
});

describe("overview", () => {
  it("shows the empty case without inventing data", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /leadtime/limits": limits, "GET /leadtime/analytics/summary": () => jsonResponse(summary({ counts: { deliveries: 0, by_state: {}, eligible_completed: 0, with_lead_time: 0, censored: 0, excluded_by_reason: {} } })) });
    renderApp("/leadtime");
    expect(await screen.findByText("No deliveries recorded yet")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(/not a guarantee/i);
    expect(await screen.findByText(/Data is separated by organisation/)).toBeInTheDocument();
  });

  it("shows statistics with denominators and the censoring warning", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /leadtime/limits": limits, "GET /leadtime/analytics/summary": () => jsonResponse(summary()) });
    renderApp("/leadtime");
    expect(await screen.findByText("30.0%")).toBeInTheDocument();
    expect(screen.getByText(/n = 90/)).toBeInTheDocument();
    expect(screen.getByText(/Unknown, never counted as on time/)).toBeInTheDocument();
    expect(screen.getByText(/censored/i, { selector: "li" })).toBeInTheDocument();
  });

  it("shows an error with retry", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /leadtime/limits": limits, "GET /leadtime/analytics/summary": () => jsonResponse({ error: { code: "boom", message: "Server trouble" } }, 500) });
    renderApp("/leadtime");
    expect(await screen.findByText("Server trouble")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("explorer", () => {
  const row = { delivery_ref: "PO-1", product_id: 1, supplier_id: 1, destination_facility_id: 1, state: "IN_TRANSIT", state_reason: null, order_at: "2026-10-18T00:00:00Z", original_promised_at: "2026-11-01T00:00:00Z", latest_promised_at: "2026-11-03T00:00:00Z", arrived_at: null, quantity: 100, unit: "EA", transport_mode: "ROAD", flags: [] };
  it("filters and pages on the server", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /leadtime/deliveries": () => jsonResponse({ ...page([row], 60), limit: 25 }) });
    renderApp("/leadtime/deliveries");
    expect(await screen.findByText("PO-1")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("State"), "IN_TRANSIT");
    await vi.waitFor(() => expect(calls.some((c) => c.path === "/leadtime/deliveries" && c.search.get("state") === "IN_TRANSIT")).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await vi.waitFor(() => expect(calls.some((c) => c.search.get("offset") === "25")).toBe(true));
  });
  it("states the empty case", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /leadtime/deliveries": () => jsonResponse(page([])) });
    renderApp("/leadtime/deliveries");
    expect(await screen.findByText("No deliveries match")).toBeInTheDocument();
  });
});

describe("analytics", () => {
  const g = (rows: unknown[]) => ({ group_by: "supplier", min_sample: 8, overall: { late_rate: 0.3, n_punctuality: 90 }, groups: rows, ranking_note: "Groups with fewer than 8 deliveries are not ranked." });
  const r = (key: string, suff: boolean) => ({ key: [key], deliveries: 40, censored: 0, n_lead_time: 40, median_lead_time: 9, p90_lead_time: 12, n_punctuality: suff ? 40 : 3, late: 5, late_rate: 0.125, late_rate_ci95: [0.05, 0.26], sufficient_sample: suff, mean_delay_when_late: 1 });
  it("labels sparse groups and insufficient trend evidence", async () => {
    mockApi({
      "GET /registry/me": me("viewer"),
      "GET /leadtime/analytics/groups": () => jsonResponse(g([r("Alder", true), r("Birch", false)])),
      "GET /leadtime/analytics/trend": () => jsonResponse({ window_days: 90, status: "insufficient_evidence", min_sample: 8, distinguishable: null, recent: { n: 2, late_rate: null, late_rate_ci95: null }, history: { n: 50, late_rate: 0.2, late_rate_ci95: [0.1, 0.3] }, note: "Both periods need at least 8 deliveries." }),
    });
    renderApp("/leadtime/analytics");
    expect(await screen.findByText("Alder")).toBeInTheDocument();
    expect(screen.getByText("Sufficient sample")).toBeInTheDocument();
    expect(screen.getByText(/Insufficient sample/)).toBeInTheDocument();
    expect(await screen.findByText(/Insufficient evidence to compare/)).toBeInTheDocument();
  });
});

describe("predictions and models", () => {
  const assessed = { id: 5, assessment_uid: "DA-1", subject: "DELIVERY", delivery_ref: "PO-OPEN", product_id: 1, supplier_id: 1, destination_facility_id: 1, prediction_point: "ORDER_PLACEMENT", as_of: "2026-10-30T00:00:00Z", status: "ASSESSED", unavailable_reason: null, estimate: { value: 10.2, unit: "CALENDAR_DAYS", method: "supplier_median", method_kind: "baseline", n: 40, interval: { lower: 8, upper: 13, level: 0.8 }, statement: "s" }, arrival: { status: "estimated", estimated_arrival: "2026-11-09T00:00:00", potential_delay_hours: 12 }, risk: { kind: "uncalibrated_score", score: 0.42, band: "medium", statement: "An uncalibrated score is not a probability." }, warnings: [], statement: "Predictions do not replace supplier confirmation.", created_at: "2026-10-30T00:00:00Z" };
  it("issues an assessment as an editor and labels the score kind", async () => {
    mockApi({
      "GET /registry/me": me("editor"),
      "GET /leadtime/assessments": () => jsonResponse(page([])),
      "POST /leadtime/assessments": () => jsonResponse(assessed, 201),
    });
    renderApp("/leadtime/predict");
    expect(await screen.findByText("No assessments yet")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Delivery reference"), "PO-OPEN");
    await userEvent.type(screen.getByLabelText("Product ID"), "1");
    await userEvent.click(screen.getByRole("button", { name: "Assess delivery" }));
    expect(await screen.findByText(/uncalibrated score\)/)).toBeInTheDocument();
    expect(screen.getByText(/42.0% — medium \(heuristic band\)/)).toBeInTheDocument();
    expect(screen.getByText(/80.0% interval 8–13/)).toBeInTheDocument();
    const body = JSON.parse(calls.find((c) => c.method === "POST")?.text ?? "{}") as Record<string, unknown>;
    expect(body).toMatchObject({ delivery_ref: "PO-OPEN", product_id: 1, prediction_point: "ORDER_PLACEMENT" });
  });
  it("hides the form from viewers", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /leadtime/assessments": () => jsonResponse(page([{ ...assessed, status: "UNAVAILABLE", unavailable_reason: "The order is CANCELLED.", estimate: null, arrival: null, risk: null }])) });
    renderApp("/leadtime/predict");
    expect(await screen.findByText(/Not available: The order is CANCELLED/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Assess delivery" })).not.toBeInTheDocument();
    expect(screen.getByText(/needs the editor role/)).toBeInTheDocument();
  });
  it("shows a declined run and model metrics for a finished one", async () => {
    const base = { run_uid: "LT-1", status: "COMPLETED", requested_by: "t", queued_at: "2026-10-30T00:00:00Z", error: null, statement: "Evidence only.", config: {}, artifact: null, splits: null, candidates: { ineligible: { logistic_late: "needs at least 10 late" } }, selection: null, calibration: null, intervals: null, explanation: null, leakage_controls: null, failures: [], statements: {} };
    mockApi({
      "GET /registry/me": me("viewer"),
      "GET /leadtime/runs/3": () => jsonResponse({ ...base, id: 3, target: "LATE_RISK", result_status: "insufficient_data", selected_model: null, selection_status: null, score_kind: null, warnings: ["No model was produced: too few late deliveries"] }),
      "GET /leadtime/runs/4": () => jsonResponse({ ...base, id: 4, target: "LEAD_TIME", result_status: "completed", selected_model: "supplier_median", selection_status: "baseline_selected", score_kind: null, warnings: [], selection: { status: "baseline_selected", selected: "supplier_median", rationale: "No learned model beat the baseline by 5%." }, evaluation: { models: { supplier_median: { validation: { mae: { value: 1.5 } }, test: { mae: { value: 1.7 } } } } } }),
    });
    const { unmount } = renderApp("/leadtime/models/3");
    expect(await screen.findByText("No model was produced")).toBeInTheDocument();
    expect(screen.getByText(/logistic_late: needs at least 10 late/)).toBeInTheDocument();
    unmount();
    renderApp("/leadtime/models/4");
    expect(await screen.findByText(/supplier_median \(selected\)/)).toBeInTheDocument();
    expect(screen.getByText(/No learned model beat the baseline/)).toBeInTheDocument();
  });
});

describe("import and impact", () => {
  it("previews without writing and keeps commit disabled until previewed", async () => {
    mockApi({
      "GET /registry/me": me("editor"),
      "POST /leadtime/imports": (url) => jsonResponse(url.searchParams.get("mode") === "preview" ? { mode: "preview", would_write: 2, summary: { rows_total: 3, rows_accepted: 2, rows_rejected: 1 }, rows: [{ row_number: 3, outcome: "REJECTED", errors: [{ code: "UNRESOLVED_PRODUCT", message: "Unknown product." }] }] } : { committed: true, created: true, replayed_existing_batch: false, batch: { id: 1, batch_uid: "B-1", rows_accepted: 2, rows_rejected: 1, rows_duplicate: 0 } }),
    });
    renderApp("/leadtime/import");
    const commit = await screen.findByRole("button", { name: "Commit import" });
    expect(commit).toBeDisabled();
    await userEvent.upload(screen.getByLabelText("CSV or JSON file"), new File(["a,b\n1,2\n"], "d.csv", { type: "text/csv" }));
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText(/UNRESOLVED_PRODUCT/)).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "POST").every((c) => c.search.get("mode") === "preview")).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Commit import" }));
    expect(await screen.findByText(/Imported batch B-1: 2 accepted/)).toBeInTheDocument();
  });
  it("explains the impact panel when no analysis is chosen", async () => {
    mockApi({ "GET /registry/me": me("viewer") });
    renderApp("/leadtime/impact");
    expect(await screen.findByText(/conditional what-if/)).toBeInTheDocument();
  });
});
