import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { Analysis, Comparison, Finding, FindingDetail, Summary } from "../lib/impactTypes";
import { tokenStore } from "../lib/registryApi";
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
const page = <T,>(items: T[], total = items.length) => ({ items, total, limit: 25, offset: 0 });

const summary = (over: Partial<Summary> = {}): Summary => ({
  scope: "Scope: confirmed relationships in the recorded network only.", reachability_note: "Reachability is not shortage.", initiating_events: 2, unique_entities: 7, initiating: 2, direct: 2,
  propagated: 3, suppliers_exposed: 2, facilities_exposed: 3, products_exposed: 2, quantified: 2, unquantified: 5, indeterminate: 0, distinct_paths_found: 9, distinct_paths_exact: true,
  multi_path_entities: 1, multi_event_entities: 1, overlapping_entities: 1, independent_entities: 1, bottleneck_candidates: 1, alternatives_total: 2, entities_with_alternatives: 1,
  counting_rule: "Headline totals count each entity once.", by_state: {}, ...over,
});
const analysis = (over: Partial<Analysis> = {}): Analysis => ({
  id: 3, analysis_uid: "IA-3", run_id: 5, scenario_id: 1, scenario_name: "Alpha outage", scenario_version: 1, status: "COMPLETED", requested_by: "tester", queued_at: "2026-10-27T10:00:00Z",
  started_at: null, finished_at: null, duration_ms: 20, algorithm_version: "ci-1", config: {}, config_hash: "abcdef0123456789", graph_hash: "0123456789abcdef", cancel_requested: false, truncated: false,
  truncation_reasons: [], error: null, complete: true, summary: summary(), coverage: null, warnings: [], failures: [], disclaimer: "d",
  reproducibility: { algorithm_version: "ci-1", config_hash: "abcdef0123456789", graph_hash: "0123456789abcdef", run_id: 5, note: "Reproducible." }, ...over,
});
const finding = (over: Partial<Finding> = {}): Finding => ({
  id: 1, entity_type: "FACILITY", entity_id: 2, entity_code: "DEMO-FAC-1", entity_name: "Plant One", classification: "DIRECT", min_depth: 1, quantification: "UNQUANTIFIED", exposure_level: null,
  impact_state: "POTENTIAL_EXPOSURE", loss_pct: null, loss_pct_lower_bound: null, added_lead_time_days: null, path_count: 1, paths_complete: true, event_uids: ["e1"], interaction: null,
  confidence: null, evidence_strength: "UNKNOWN", coverage: 0, key_uncertainty: "Supply share is unknown.", uncertainty_count: 1, evidence_count: 1, alternatives_count: 0, dimension_status: {},
  potential_exposure: true, ...over,
});
const detail = (f: Finding): FindingDetail => ({
  ...f, paths: [{ text: "Supplier A → Plant One" }], per_event: [], combined: null,
  dimensions: { supply_shortfall: { status: "UNAVAILABLE", reason: "Supply share is not recorded." }, facility_exposure: { status: "QUALITATIVE", note: "Reachable." } },
  uncertainty: [{ code: "SHARE_UNKNOWN", text: "Supply share is unknown." }], evidence: [{ ref: "SUPPLY:1", supply_share_pct: null, lead_time_days: null }], assumptions: ["No inventory buffer is modelled."],
  rules: [], explanation: "Receives from the disrupted supplier.",
});
const summaryResp = (a: Analysis) => ({
  analysis_id: a.id, status: a.status, summary: a.summary, coverage: { findings_with_inputs_applicable: 5, mean_input_coverage: 0.4, full_coverage: 1, missing_inputs_by_field: {}, evidence_strength: {}, note: "" },
  uncertainty: [{ code: "SHARE_UNKNOWN", count: 4, text: "Supply share is unknown." }], excluded: {}, warnings: [], truncated: a.truncated, truncation_reasons: a.truncation_reasons, disclaimer: "d",
});
const routes = (a: Analysis, items: Finding[] = [finding()]): Record<string, import("vitest").Mock | Handler> => ({
  "GET /registry/me": me("viewer"),
  [`GET /impact/analyses/${a.id}`]: () => jsonResponse(a),
  [`GET /impact/analyses/${a.id}/summary`]: () => jsonResponse(summaryResp(a)),
  [`GET /impact/analyses/${a.id}/findings`]: () => jsonResponse({ ...page(items), analysis_status: a.status, complete: a.complete }),
  [`GET /impact/analyses/${a.id}/findings/FACILITY/2`]: () => jsonResponse(detail(items[0] ?? finding())),
  [`GET /impact/analyses/${a.id}/graph`]: () => jsonResponse({ nodes: items, edges: [], total_nodes: items.length, shown_nodes: items.length, truncated: false, note: "Bounded view." }),
  [`GET /impact/analyses/${a.id}/criticality`]: () => jsonResponse({ ...page([]), note: "Structural only." }),
  [`GET /impact/analyses/${a.id}/alternatives`]: () => jsonResponse({ ...page([]), notice: "Alternates are not validated." }),
});

describe("impact analyses list", () => {
  it("shows an empty state and a disabled start for viewers", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /impact/analyses": () => jsonResponse(page([])) });
    renderApp("/impact");
    expect(await screen.findByText("No cascading analyses yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start analysis" })).toBeDisabled();
  });

  it("shows an error with retry", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /impact/analyses": () => jsonResponse({ error: { code: "internal", message: "Boom" } }, 500) });
    renderApp("/impact");
    expect(await screen.findByText(/Boom/)).toBeInTheDocument();
  });

  it("an unauthorized response is shown, not hidden", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /impact/analyses": () => jsonResponse({ error: { code: "unauthorized", message: "A valid token is required." } }, 401) });
    renderApp("/impact");
    expect(await screen.findByText(/valid token is required/i)).toBeInTheDocument();
  });

  it("an editor can start an analysis and is taken to it", async () => {
    const a = analysis();
    mockApi({ ...routes(a), "GET /registry/me": me("editor"), "GET /impact/analyses": () => jsonResponse(page([])), "POST /impact/analyses": () => jsonResponse(a, 202) });
    renderApp("/impact?run_id=5");
    const btn = await screen.findByRole("button", { name: "Start analysis" });
    await waitFor(() => expect(btn).toBeEnabled());
    await userEvent.click(btn);
    expect(await screen.findByRole("heading", { name: "Analysis #3" })).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ run_id: 5 });
  });
});

describe("analysis page", () => {
  it("renders the overview with the scope statement and counts each entity once", async () => {
    const a = analysis();
    mockApi(routes(a));
    renderApp("/impact/3");
    expect(await screen.findByText(/Scope: confirmed relationships/)).toBeInTheDocument();
    expect(await screen.findByText("Unique entities reached")).toBeInTheDocument();
    expect(screen.getAllByText(/not necessarily safe/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/never added into totals/i)).toBeInTheDocument();
  });

  it("flags a partial analysis and the limit that was hit", async () => {
    const a = analysis({ status: "PARTIAL", complete: false, truncated: true, truncation_reasons: ["MAX_NODES"] });
    mockApi(routes(a));
    renderApp("/impact/3");
    expect(await screen.findByText(/Partial result/)).toBeInTheDocument();
    expect(screen.getByText(/MAX_NODES/)).toBeInTheDocument();
  });

  it("shows a failed analysis with its safe error and no result tabs", async () => {
    const a = analysis({ status: "FAILED", complete: false, summary: null, error: "The analysis could not be completed." });
    mockApi(routes(a));
    renderApp("/impact/3");
    expect(await screen.findByText(/analysis failed/i)).toBeInTheDocument();
    expect(screen.getByText(/could not be completed/)).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Findings" })).not.toBeInTheDocument();
  });

  it("shows a cancelled analysis without results", async () => {
    mockApi(routes(analysis({ status: "CANCELLED", complete: false, summary: null })));
    renderApp("/impact/3");
    expect(await screen.findByText(/was cancelled/i)).toBeInTheDocument();
  });

  it("a missing analysis shows the API error", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /impact/analyses/9": () => jsonResponse({ error: { code: "not_found", message: "Analysis not found." } }, 404) });
    renderApp("/impact/9");
    expect(await screen.findByText(/Analysis not found/)).toBeInTheDocument();
  });

  it("findings: potential exposure is labelled and unknown loss is not shown as 0", async () => {
    const a = analysis();
    mockApi(routes(a));
    renderApp("/impact/3");
    await userEvent.click(await screen.findByRole("tab", { name: "Findings" }));
    const row = (await screen.findByText("Plant One")).closest("tr") as HTMLElement;
    expect(within(row).getByText(/Potential exposure \(not quantified\)/)).toBeInTheDocument();
    const loss = within(row).getByText("Unknown");
    expect(loss.closest("td")).toHaveTextContent(/^Unknown$/);
  });

  it("findings: a lower bound is worded as such and filters are sent to the server", async () => {
    const a = analysis();
    mockApi(routes(a, [finding({ loss_pct_lower_bound: 30, impact_state: "ASSUMPTION_DEPENDENT" })]));
    renderApp("/impact/3");
    await userEvent.click(await screen.findByRole("tab", { name: "Findings" }));
    expect(await screen.findByText(/at least 30%/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Classification"), "PROPAGATED");
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/findings") && c.search.get("classification") === "PROPAGATED")).toBe(true));
  });

  it("findings: an empty result says unlisted does not mean safe", async () => {
    const a = analysis();
    mockApi(routes(a, []));
    renderApp("/impact/3");
    await userEvent.click(await screen.findByRole("tab", { name: "Findings" }));
    expect(await screen.findByText("No entities match")).toBeInTheDocument();
    expect(screen.getAllByText(/not necessarily safe/i).length).toBeGreaterThan(1);
  });

  it("opening a finding shows dimensions with explicit unavailable states, assumptions and paths", async () => {
    const a = analysis();
    mockApi(routes(a));
    renderApp("/impact/3");
    await userEvent.click(await screen.findByRole("tab", { name: "Findings" }));
    await userEvent.click(await screen.findByRole("button", { name: "Explain Plant One" }));
    const panel = await screen.findByRole("complementary", { name: /How Plant One is affected/ });
    expect(await within(panel).findByText("Unavailable (data missing)")).toBeInTheDocument();
    expect(within(panel).getByText("Qualitative only")).toBeInTheDocument();
    expect(within(panel).getByText(/No inventory buffer is modelled/)).toBeInTheDocument();
    expect(within(panel).getByText(/Supplier A → Plant One/)).toBeInTheDocument();
  });

  it("explorer: tiers are listed with a table equivalent for relationships", async () => {
    const a = analysis();
    mockApi(routes(a, [finding({ classification: "INITIATING", min_depth: 0, entity_name: "Supplier A", entity_type: "SUPPLIER", impact_state: "SCENARIO_ASSUMED" }), finding({ id: 2 })]));
    renderApp("/impact/3");
    await userEvent.click(await screen.findByRole("tab", { name: "Explorer" }));
    expect(await screen.findByRole("region", { name: "Initiating tier" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Tier 1" })).toBeInTheDocument();
    expect(screen.getByText(/table equivalent/i)).toBeInTheDocument();
    expect(screen.getByText(/not a confirmed shortage/i)).toBeInTheDocument();
  });

  it("bottlenecks: the empty state does not claim safety", async () => {
    mockApi(routes(analysis()));
    renderApp("/impact/3");
    await userEvent.click(await screen.findByRole("tab", { name: "Bottlenecks" }));
    expect(await screen.findByText("No structural bottleneck candidates")).toBeInTheDocument();
  });

  it("alternatives: shows the not-validated notice", async () => {
    mockApi(routes(analysis()));
    renderApp("/impact/3");
    await userEvent.click(await screen.findByRole("tab", { name: "Alternatives" }));
    expect(await screen.findByText(/Alternates are not validated/)).toBeInTheDocument();
  });
});

describe("compare", () => {
  const cmp = (over: Partial<Comparison> = {}): Comparison => ({
    analysis_ids: [3, 4], baseline_analysis_id: 3, comparable: true, not_comparable_reasons: [],
    warnings: [{ code: "NETWORK_INPUTS_DIFFER", text: "The network data read by these analyses differs." }],
    runs: [3, 4].map((id) => ({ analysis_id: id, run_id: id, status: "COMPLETED" as const, unique_entities: id * 2, initiating: 1, direct: 1, propagated: 1, suppliers_exposed: 1, facilities_exposed: 1, products_exposed: 1, quantified: 1, unquantified: 1, bottleneck_candidates: 0, mean_input_coverage: 0.5, truncated: false })),
    comparisons: [{ baseline_analysis_id: 3, other_analysis_id: 4, newly_exposed: { total: 1, items: [{ entity: "FACILITY:9" }] }, no_longer_exposed: { total: 0, items: [] }, persistently_exposed: 5, depth_changes: { total: 0 }, quantified_changes: { total: 0 }, quantification_status_changes: { total: 0 }, counts: {}, coverage_delta: 0, interpretation: "Cannot separate real change from data change when a warning is present." }],
    note: "Entities are compared by identity.", ...over,
  });
  it("needs at least two analyses", async () => {
    mockApi({ "GET /registry/me": me("viewer") });
    renderApp("/impact/compare?ids=3");
    expect(await screen.findByText("Choose analyses to compare")).toBeInTheDocument();
  });
  it("shows comparability warnings above the numbers", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /impact/analyses/compare": () => jsonResponse(cmp()) });
    renderApp("/impact/compare?ids=3,4");
    expect(await screen.findByText(/network data read by these analyses differs/)).toBeInTheDocument();
    expect(screen.getByText(/\(baseline\)/)).toBeInTheDocument();
    expect(screen.getByText(/Newly exposed/)).toBeInTheDocument();
  });
  it("incomparable analyses are refused, with the reason, and no change lists", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /impact/analyses/compare": () => jsonResponse(cmp({ comparable: false, comparisons: [], not_comparable_reasons: [{ code: "ALGORITHM_DIFFERS", text: "different algorithm versions" }] })) });
    renderApp("/impact/compare?ids=3,4");
    expect(await screen.findByText(/not directly comparable/i)).toBeInTheDocument();
    expect(screen.getByText(/different algorithm versions/)).toBeInTheDocument();
    expect(screen.queryByText(/Newly exposed/)).not.toBeInTheDocument();
  });
});
