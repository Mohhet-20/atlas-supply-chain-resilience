import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ForecastChart } from "../components/forecast/Bits";
import type { ForecastValue, MetricSet, RunDetail, RunSummary } from "../lib/forecastTypes";
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
const mset = (v: number | null): MetricSet => {
  const m = { value: v, n: 10, note: null };
  return { mae: m, rmse: m, wape: m, mase: m, bias: m, smape: m };
};
const run = (over: Partial<RunSummary> = {}): RunSummary => ({
  id: 7, run_uid: "FC-7", status: "COMPLETED", result_status: "completed", requested_by: "tester", product_id: 1, facility_id: 2, frequency: "daily", horizon: 3, unit: "EA",
  origin: "2026-10-20", selected_model: "seasonal_naive", model_version: "1", algorithm_version: "fc-1", config_hash: "abc", data_snapshot_hash: "0123456789abcdef", provenance: "MODEL_ESTIMATE",
  queued_at: "2026-10-20T10:00:00Z", finished_at: "2026-10-20T10:00:01Z", duration_ms: 5, error: null, fallback: null, selection_status: "selected", patterns: ["seasonal"], disclaimer: "d", ...over,
});
const detail = (over: Partial<RunDetail> = {}): RunDetail => ({
  ...run(), config: { frequency: "daily", horizon: 3, lookback_periods: 365, origin: null, basis: "AUTO", unit: null, seasonal_period: null, candidates: null, n_folds: 5, interval_levels: [0.8], missing_policy: "interpolate_linear", outlier_handling: "flag", selection_metric: "mase", nonnegative: true },
  model_params: {}, training_interval: { start: "2026-01-01", end_exclusive: "2026-10-20" }, preparation: { status: "ok", counts: {}, imputations: [], outliers: [], warnings: [], steps: [] },
  diagnostics: null, patterns_detail: { labels: ["seasonal"], evidence: {}, criteria: {} }, candidates: { evaluated: ["naive", "seasonal_naive"], ineligible: {} },
  selection: { status: "selected", selected: "seasonal_naive", metric: "mase", rationale: "Ranked by mase over 5 rolling-origin fold(s).", ranking: [] },
  validation: { scheme: "expanding-window rolling origin", fold_train_lengths: [100, 101], evaluation_horizon: 3, configured_horizon: 3, status: "ok", note: null, leakage_controls: ["x"] },
  clamping: { enabled: true, points_clamped: 1, bounds_clamped: 0, total_point_shift: 0.5 }, reproducibility: {}, timings: {}, warnings: [], statements: {}, ...over,
});
const val = (step: number, point: number, ints: ForecastValue["intervals"] = []): ForecastValue => ({
  step, period_start: `2026-10-${19 + step}`, period_end: `2026-10-${20 + step}`, point, point_raw: point, clamped_at_zero: false, intervals: ints, unit: "EA", model: "seasonal_naive", model_version: "1", origin: "2026-10-20", kind: "forecast",
});
const sup = (lv: number, lo: number, hi: number) => ({ level: lv, lower: lo, upper: hi, status: "supported" as const, n: 12 });

const runRoutes = (r: RunDetail, values: ForecastValue[], extra: Record<string, Handler> = {}): Record<string, Handler> => ({
  "GET /registry/me": me("editor"),
  [`GET /forecast/runs/${r.id}`]: () => jsonResponse(r),
  [`GET /forecast/runs/${r.id}/values`]: () => jsonResponse({ run: run(), items: values, total: values.length, disclaimer: "d", note: null }),
  [`GET /forecast/runs/${r.id}/intervals`]: () => jsonResponse({ calibration: [{ level: 0.8, supported: true, method: "empirical rolling-origin residual quantiles", coverage_evaluation: { status: "evaluated", n_scored: 40, empirical_coverage: 0.82, mean_width: 11.4, note: "n" } }], statement: "Intervals are empirical.", assumptions: [] }),
  [`GET /forecast/runs/${r.id}/metrics`]: () => jsonResponse({ models: { naive: { overall: mset(1.2), per_fold: {}, per_horizon: {}, complete: true, failed_folds: 0 }, seasonal_naive: { overall: mset(0.5), per_fold: {}, per_horizon: {}, complete: true, failed_folds: 0 }, lag_ridge: { overall: mset(null), per_fold: {}, per_horizon: {}, complete: false, failed_folds: 2 } }, ranking: [], selected: "seasonal_naive" }),
  [`GET /forecast/runs/${r.id}/forecast-vs-actual`]: () => jsonResponse({ matches: [], counts: { final: 0, preliminary: 0, partial: 0, missing: 3 }, metrics: null, note: "The issued forecast is never changed." }),
  "GET /forecast/series/diagnostics": () => jsonResponse({ preparation: { status: "ok", counts: {}, warnings: [], unit: "EA", basis: "CONSUMPTION" }, points: [{ period_start: "2026-10-18", raw: 10, value: 10, observed: true, imputed: false }, { period_start: "2026-10-19", raw: null, value: 11, observed: false, imputed: true }], diagnostics: null, patterns: { labels: ["seasonal"], evidence: {}, criteria: {} }, statements: {} }),
  ...extra,
});

describe("chart", () => {
  it("draws history, forecast and intervals with a text alternative and says when no interval is supported", () => {
    const fc = [val(1, 10, [sup(0.8, 8, 12)]), val(2, 11, [sup(0.8, 9, 13)])];
    const { rerender } = render(<ForecastChart history={[{ period_start: "a", value: 5, imputed: false }, { period_start: "b", value: 6, imputed: true }]} forecast={fc} unit="EA" origin="2026-10-20" model="naive" />);
    const img = screen.getByRole("img");
    expect(img).toHaveAttribute("aria-label", expect.stringContaining("80% prediction intervals"));
    expect(screen.getByText(/Forecast \(estimate\)/)).toBeInTheDocument();
    expect(screen.getByText(/Imputed history/)).toBeInTheDocument();
    rerender(<ForecastChart history={[]} forecast={[val(1, 10), val(2, 11)]} unit="EA" origin="2026-10-20" model="naive" />);
    expect(screen.getByRole("img")).toHaveAttribute("aria-label", expect.stringContaining("No prediction interval is shown"));
    expect(screen.getByText(/No prediction interval \(not supported/)).toBeInTheDocument();
  });
});

describe("overview and series", () => {
  it("states the empty case without inventing data", async () => {
    mockApi({
      "GET /registry/me": me("viewer"),
      "GET /forecast/limits": () => jsonResponse({ algorithm_version: "fc-1", max_horizon_daily: 90, max_horizon_weekly: 26, max_history_periods: 730, max_folds: 12, statements: { fit: "A good fit is not a good forecast.", interval: "i", decline: "d", forecast: "f" }, organisation_model: "none — ATLAS has no tenancy" }),
      "GET /forecast/series": () => jsonResponse(page([])),
      "GET /forecast/runs": () => jsonResponse(page([])),
    });
    renderApp("/forecast");
    expect(await screen.findByText("No demand series yet")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(/not an observation/i);
    expect(await screen.findByText(/A good fit is not a good forecast/)).toBeInTheDocument();
    expect(screen.getByText(/ATLAS has no tenancy/)).toBeInTheDocument();
  });

  it("pages and searches series on the server", async () => {
    const item = { product_id: 1, facility_id: 2, product_code: "P-1", product_name: "Elm Widget", facility_code: "F-1", facility_name: "Cedar Plant", observations: 120, first_period: "2026-01-01", last_period: "2026-10-20", kinds: ["CONSUMPTION"], units: ["EA"], provenance: ["SYNTHETIC_DEMO"], latest_run: null };
    const spy = mockApi({ "GET /registry/me": me("viewer"), "GET /forecast/series": () => jsonResponse({ ...page([item], 60), limit: 25 }) });
    renderApp("/forecast/series");
    expect(await screen.findByText("Elm Widget")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search product or facility"), "elm");
    await vi.waitFor(() => expect(calls.some((c) => c.path === "/forecast/series" && c.search.get("q") === "elm")).toBe(true));
    expect(calls[calls.length - 1]?.search.get("limit")).toBe("25");
    spy.mockRestore();
  });

  it("shows an error with retry", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /forecast/series": () => jsonResponse({ error: { code: "boom", message: "Server trouble" } }, 500) });
    renderApp("/forecast/series");
    expect(await screen.findByText("Server trouble")).toBeInTheDocument();
  });
});

describe("series detail", () => {
  const diag = {
    preparation: { status: "ok", counts: {}, warnings: [], unit: "EA", basis: "CONSUMPTION" }, points: [],
    diagnostics: { n: 120, mean: 50, median: 50, std: 2, cv: null, cv_note: "undefined: mean is zero or fewer than 8 periods", zero_proportion: 0, missing_proportion: 0.025, coverage: 1, trend: { relative_drift: null, t_stat: null, note: "undefined" }, seasonality: { period: 7, acf: null }, recent_vs_historical: { relative_change: null }, outliers: { count: 1 } },
    patterns: { labels: ["stable"], evidence: {}, criteria: { stable: "none of the above and CV <= 0.3" } }, statements: {},
  };
  it("shows undefined diagnostics as undefined, validates, previews and starts a run for editors", async () => {
    mockApi({
      "GET /registry/me": me("editor"),
      "GET /forecast/series/diagnostics": () => jsonResponse(diag),
      "POST /forecast/config/validate": () => jsonResponse({ valid: true, errors: [], config: {} }),
      "POST /forecast/candidates/preview": () => jsonResponse({ preparation_status: "ok", eligible: ["naive", "ses"], ineligible: { croston_sba: "series is not intermittent" }, validation_plan: { folds: 5, evaluation_horizon: 14, min_train_periods: 60, status: "ok", note: null } }),
      "POST /forecast/runs": () => jsonResponse(run({ id: 9, status: "QUEUED", result_status: null }), 202),
      "GET /forecast/runs/9": () => jsonResponse(detail({ id: 9, status: "QUEUED", result_status: null })),
    });
    renderApp("/forecast/series/1/2");
    expect(await screen.findByText("Diagnostics")).toBeInTheDocument();
    expect(screen.getByText(/undefined: mean is zero/)).toBeInTheDocument();
    expect(screen.getAllByText("undefined").length).toBeGreaterThanOrEqual(3);
    const runBtn = screen.getByRole("button", { name: "Run forecast" });
    expect(runBtn).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Validate and preview models" }));
    expect(await screen.findByText(/Validation plan: 5 fold\(s\)/)).toBeInTheDocument();
    expect(screen.getByText(/Not eligible: croston_sba/)).toBeInTheDocument();
    await userEvent.click(runBtn);
    expect(await screen.findByText(/Run #9/)).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST" && c.path === "/forecast/runs");
    expect(JSON.parse(post?.text ?? "{}")).toMatchObject({ product_id: 1, facility_id: 2, config: { frequency: "daily", horizon: 28 } });
  });

  it("does not let a viewer start a run and explains why", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /forecast/series/diagnostics": () => jsonResponse(diag) });
    renderApp("/forecast/series/1/2");
    expect(await screen.findByText(/needs the/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run forecast" })).toBeDisabled();
  });

  it("explains insufficient history instead of showing numbers", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /forecast/series/diagnostics": () => jsonResponse({ preparation: { status: "insufficient_data", counts: {}, warnings: ["8 periods of history; at least 14 are needed."], unit: "KG", basis: "CONSUMPTION" }, diagnostics: null, patterns: { labels: ["insufficient_history"], evidence: {}, criteria: {} }, statements: {} }) });
    renderApp("/forecast/series/3/4");
    expect(await screen.findByText("Not enough usable history")).toBeInTheDocument();
    expect(screen.getByText(/Insufficient History/)).toBeInTheDocument();
  });
});

describe("run page", () => {
  it("shows forecast, interval evidence, undefined metrics and unranked failed models", async () => {
    const values = [val(1, 10.5, [sup(0.8, 8, 12)]), val(2, 11, [sup(0.8, 9, 13)]), val(3, 12, [])];
    mockApi(runRoutes(detail(), values));
    renderApp("/forecast/runs/7");
    expect(await screen.findByText("Historical vs forecast")).toBeInTheDocument();
    expect(await screen.findByText(/80%: 8–12/)).toBeInTheDocument();
    expect(screen.getByText("not supported by the evidence")).toBeInTheDocument();
    expect(await screen.findByText(/empirical coverage 82% over 40 out-of-sample points/)).toBeInTheDocument();
    const row = screen.getByText("lag_ridge").closest("tr")!;
    expect(within(row).getAllByText("undefined").length).toBeGreaterThan(0);
    expect(within(row).getByText(/failed in 2 fold\(s\); not ranked/)).toBeInTheDocument();
    expect(screen.getByText(/seasonal_naive \(selected\)/)).toBeInTheDocument();
    expect(screen.getByText(/Ranked by mase/)).toBeInTheDocument();
    expect(screen.getByText(/No final actuals yet, so nothing is scored/)).toBeInTheDocument();
    expect(screen.getByText(/1 point\(s\) and 0 bound\(s\) raised to zero/)).toBeInTheDocument();
  });

  it("labels a fallback and never claims validation", async () => {
    mockApi(runRoutes(detail({ result_status: "completed_with_fallback", status: "PARTIAL", fallback: { used: "moving_average", reason: "only 2 fold(s) available", validated: false } }), [val(1, 10)]));
    renderApp("/forecast/runs/7");
    expect(await screen.findByText(/Labelled fallback: moving_average/)).toBeInTheDocument();
    expect(screen.getByText(/no validated accuracy claim/)).toBeInTheDocument();
  });

  it("explains a declined run", async () => {
    mockApi(runRoutes(detail({ result_status: "insufficient_data", selected_model: null, warnings: ["No forecast was produced: history too short"] }), []));
    renderApp("/forecast/runs/7");
    expect(await screen.findByText("ATLAS declined to forecast this series")).toBeInTheDocument();
  });

  it("starts a stockout analysis from the forecast with the run number", async () => {
    mockApi(runRoutes(detail(), [val(1, 10)], {
      "POST /inventory/analyses": () => jsonResponse({ id: 31, status: "QUEUED", config: {} }, 202),
      "GET /inventory/analyses/31": () => jsonResponse({ id: 31, analysis_uid: "SA-31", kind: "BASELINE", status: "QUEUED", requested_by: "t", impact_analysis_id: null, config: {}, config_hash: "", algorithm_version: "inv-1", as_of: "2026-10-27T10:00:00Z", input_hash: null, queued_at: "2026-10-27T10:00:00Z", started_at: null, finished_at: null, duration_ms: null, truncated: false, truncation_reasons: [], error: null, disclaimer: "d" }),
    }));
    renderApp("/forecast/runs/7");
    await userEvent.click(await screen.findByRole("button", { name: "Run stockout analysis with this forecast" }));
    await vi.waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/inventory/analyses")).toBe(true));
    const post = calls.find((c) => c.path === "/inventory/analyses" && c.method === "POST");
    expect(JSON.parse(post?.text ?? "{}")).toMatchObject({ config: { forecast_run_id: 7 } });
  });

  it("shows a queued run as pending with a refresh control", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /forecast/runs/7": () => jsonResponse(detail({ status: "RUNNING", result_status: null, selected_model: null, selection: null })) });
    renderApp("/forecast/runs/7");
    expect(await screen.findByText(/This run is running/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });
});

describe("runs list and monitoring", () => {
  it("filters runs by status on the server", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /forecast/runs": () => jsonResponse(page([run()])) });
    renderApp("/forecast/runs");
    expect(await screen.findByText("#7")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Status"), "FAILED");
    await vi.waitFor(() => expect(calls.some((c) => c.path === "/forecast/runs" && c.search.get("status") === "FAILED")).toBe(true));
  });

  it("runs a monitoring check and says it never retrains", async () => {
    mockApi({
      "GET /registry/me": me("editor"),
      "GET /forecast/alerts": () => jsonResponse(page([])),
      "POST /forecast/monitoring/check": () => jsonResponse({ count: 0, note: "Monitoring raises signals only. It never retrains, replaces or edits a forecast." }),
    });
    renderApp("/forecast/monitoring");
    expect(await screen.findByText("No monitoring alerts")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Check latest forecasts now" }));
    expect(await screen.findByText(/never retrains, replaces or edits/)).toBeInTheDocument();
  });
});
