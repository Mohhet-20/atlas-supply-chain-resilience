import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { tokenStore } from "../lib/registryApi";
import type { Alert, AssessmentDetail, Comparison, Factor, Overview, Run, SupplierRisk, SupplierRow } from "../lib/riskTypes";
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
const interp = { meaning: "A rule-based indicator.", not_a_probability: true, not_a_prediction: "Not a prediction.", missing_data: "Missing is not low." };

const overview = (over: Partial<Overview> = {}): Overview => ({
  generated_at: "2026-10-22T10:00:00Z", methodology_version: "rm-1", counts_note: "Counts are exact.", suppliers_in_scope: 6, assessed_suppliers: 6, not_assessed: 0,
  by_status: { ASSESSED: 4, PARTIAL: 2, INSUFFICIENT_EVIDENCE: 0 }, by_classification: { LOW: 2, MODERATE: 2, HIGH: 0, CRITICAL: 0 }, classified: 4, insufficient_evidence: 0,
  requires_review_unreviewed: 1, high_dependency_exposure: 0, with_single_source_dependency: 3, aging_assessments: 0, assessment_stale_days: 30, alerts_open_by_type: {},
  mixed_methodology: false, methodology_versions_in_use: { "rm-1": 6 }, mean_evidence_coverage: 0.52, max_possible_coverage: 0.65,
  run_health: { recent_runs: 1, last_run: null, last_run_incomplete: false },
  unavailable_categories: [{ category: "OPERATIONAL", label: "Operational exposure", data_dependency: "A connector for verified disruption data." }], ...over,
});
const row = (over: Partial<SupplierRow> = {}): SupplierRow => ({
  supplier_id: 1, supplier_code: "DEMO-SUP-001", name: "Synthetic Supplier Alpha", country_or_region: "Asia", category: "Parts", supplier_status: "ACTIVE", provenance: "SYNTHETIC_DEMO",
  assessment_status: "ASSESSED", risk_score: 33.12, classification: "MODERATE", evidence_coverage: 0.65, reliability: 0.24, reliability_band: "LOW", assessment_id: 1,
  computed_at: "2026-10-22T10:00:00Z", last_confirmed_at: "2026-10-22T10:00:00Z", review_state: "UNREVIEWED", requires_review: true, methodology_version: "rm-1", rank_comparable: true,
  top_factors: [], contains_synthetic: true, override: null, ...over,
});
const page = (items: SupplierRow[], total = items.length) => ({ items, total, limit: 25, offset: 0, sort: "score_desc", ordering_note: "Unscored suppliers sort last and are never ranked safer." });
const factor = (over: Partial<Factor> = {}): Factor => ({
  id: 1, category: "CONCENTRATION", label: "Concentration risk", role: "risk", state: "SCORED", basis: "INFERRED", score: 44.44, severity: "MODERATE", weight: 0.15,
  explanation: "Holds a 44% weighted share.", unavailable_reason: null, missing_inputs: [], evidence_observed_at: null, freshness: "FRESH", confidence: 0.18, quality_context: {},
  requires_review: false, details: {}, method_version: "rm-1", data_available: true,
  evidence: [{ evidence_type: "RELATIONSHIP", ref: "PRODUCT:1", summary: "SUPPLIES_PRODUCT DEMO-PL-001", observed_at: null, provenance: "SYNTHETIC_DEMO", source_namespace: "atlas-demo", reasoning: "Share is 50% of 2 suppliers." }],
  ...over,
});
const assessment = (over: Partial<AssessmentDetail> = {}): AssessmentDetail => ({
  id: 7, assessment_uid: "RA-1", supplier_id: 1, run_id: 1, computed_at: "2026-10-22T10:00:00Z", status: "ASSESSED", methodology_version: "rm-1", methodology_hash: "abc", risk_score: 33.12,
  classification: "MODERATE", evidence_coverage: 0.65, effective_weight: 0.65, reliability: 0.24, reliability_band: "LOW", scored_factors: 5,
  contributions: [{ category: "CONCENTRATION", score: 44.44, weight: 0.15, group: "supply_structure", counted: true, points: 27, note: null }],
  missing: [{ category: "COMPLIANCE", role: "risk", reason: "No verified compliance data source.", kind: "no_source", missing_inputs: [], data_dependency: "A compliance connector.", weight: 0.1 }],
  actions: ["Run a data-quality evaluation for this supplier."], data_basis: { contains_synthetic_or_simulated: true, max_possible_coverage: 0.65 }, evidence_latest_at: null,
  evidence_earliest_at: null, requires_review: true, review_state: "UNREVIEWED", is_current: true, superseded_at: null, supersedes_id: null, last_confirmed_at: "2026-10-22T10:00:00Z",
  rank_comparable: true, interpretation: interp, factors: [factor(), factor({ id: 2, category: "FINANCIAL", label: "Financial risk", state: "UNAVAILABLE", score: null, evidence: null, unavailable_reason: "No financial data source.", confidence: null })], ...over,
});
const supplierRisk = (a: AssessmentDetail | null, over: Partial<SupplierRisk> = {}): SupplierRisk => ({
  supplier: { id: 1, supplier_code: "DEMO-SUP-001", name: "Synthetic Supplier Alpha", country_or_region: "Asia", category: "Parts", status: "ACTIVE", provenance: "SYNTHETIC_DEMO" },
  assessed: a !== null, assessment: a, override: null, reviews: [], alerts: [], unassessed_note: a ? null : "This supplier has not been assessed yet.", ...over,
});
const alert = (over: Partial<Alert> = {}): Alert => ({
  id: 3, supplier_id: 1, supplier_name: "Synthetic Supplier Alpha", assessment_id: 7, alert_type: "NEW_SINGLE_SOURCE", severity: "WARNING", status: "OPEN", reason: "Now the only confirmed supplier of a node.",
  evidence: {}, run_id: 1, created_at: "2026-10-22T10:00:00Z", updated_at: "2026-10-22T10:00:00Z", last_seen_at: "2026-10-22T10:00:00Z", occurrences: 2, resolved_at: null, resolved_by: null,
  resolution_note: null, version: 1, delivery: "IN_APP_ONLY", ...over,
});
const run = (over: Partial<Run> = {}): Run => ({
  id: 4, run_uid: "RR-4", scope_type: "NETWORK", scope_ref: {}, status: "PARTIAL", trigger: "API", reason: null, requested_by: "tester", idempotency_key: null, queued_at: "2026-10-22T10:00:00Z",
  started_at: null, finished_at: null, duration_ms: 100, methodology_version: "rm-1", methodology_hash: "abc", graph_snapshot: {}, examined: 6, completed_assessments: 5, assessed: 3, partial: 2,
  insufficient_evidence: 0, created: 5, unchanged: 0, failed: 1, skipped: 0, alerts_created: 0, alerts_updated: 0, failures: [{ supplier_id: 9, error: "boom" }], truncated: false,
  truncation_reasons: [], error: null, ...over,
});
const base = (role = "admin", writes = true): Record<string, Handler> => ({
  "GET /registry/me": me(role, writes),
  "GET /risk/geography": () => jsonResponse({ regions: [{ region: "Asia", key: "asia", suppliers: 2, facilities: 1, high_or_critical: 0, unscored: 1, verified_coordinates: 0, missing_or_placeholder_coordinates: 1, supplier_share: 0.33, facility_share: 0.2 }], total_suppliers: 6, total_facilities: 5, top_region_supplier_share: 0.33, caveats: ["Regions are free-text labels."] }),
});

describe("risk overview", () => {
  it("separates classification from partial results and names categories without data", async () => {
    mockApi({ ...base(), "GET /risk/overview": () => jsonResponse(overview()) });
    renderApp("/risk");
    expect(await screen.findByText("6 / 6")).toBeInTheDocument();
    expect(screen.getByText(/2 partial \(score without classification\)/)).toBeInTheDocument();
    expect(screen.getByText(/never counted as low risk/)).toBeInTheDocument();
    expect(screen.getByText("Operational exposure")).toBeInTheDocument();
    expect(screen.getByText(/not a probability of failure/i)).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Asia" })).toHaveAttribute("href", "/risk/suppliers?region=Asia");
  });

  it("shows an empty state when nothing is assessed", async () => {
    mockApi({ ...base(), "GET /risk/overview": () => jsonResponse(overview({ assessed_suppliers: 0, not_assessed: 6, classified: 0, mean_evidence_coverage: null, by_classification: { LOW: 0, MODERATE: 0, HIGH: 0, CRITICAL: 0 } })) });
    renderApp("/risk");
    expect(await screen.findByText("No supplier has been assessed yet")).toBeInTheDocument();
  });

  it("shows a permission error instead of data", async () => {
    mockApi({ ...base("viewer"), "GET /risk/overview": () => jsonResponse({ error: { code: "forbidden", message: "Not allowed." } }, 403) });
    renderApp("/risk");
    expect(await screen.findByRole("alert")).toHaveTextContent("Not allowed.");
  });

  it("reports a failed load with a retry", async () => {
    mockApi({ ...base(), "GET /risk/overview": () => jsonResponse({ error: { code: "internal_error", message: "Boom." } }, 500) });
    renderApp("/risk");
    expect(await screen.findByRole("alert")).toHaveTextContent("Boom.");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("adds a navigation entry", async () => {
    mockApi({ ...base(), "GET /risk/overview": () => jsonResponse(overview()) });
    renderApp("/risk");
    expect(await screen.findByRole("link", { name: "Supplier risk" })).toHaveAttribute("href", "/risk");
  });
});

describe("suppliers table", () => {
  it("never renders a missing score as zero or low and flags synthetic data", async () => {
    mockApi({
      ...base(),
      "GET /risk/suppliers": () =>
        jsonResponse(page([row(), row({ supplier_id: 2, name: "Synthetic Supplier Bravo", assessment_status: "INSUFFICIENT_EVIDENCE", risk_score: null, classification: null, evidence_coverage: 0.1 }), row({ supplier_id: 3, name: "Synthetic Supplier Echo", assessment_status: "PARTIAL", risk_score: 50, classification: null })])),
    });
    renderApp("/risk/suppliers");
    expect(await screen.findByText("Synthetic Supplier Bravo")).toBeInTheDocument();
    expect(screen.getAllByText("Insufficient evidence").length).toBeGreaterThan(0);
    expect(screen.getByText("Not classified (partial)")).toBeInTheDocument();
    expect(screen.getByLabelText("No score")).toHaveTextContent("—");
    expect(screen.getByText(/Synthetic demo evidence/)).toBeInTheDocument();
    expect(screen.getByText(/never ranked safer/)).toBeInTheDocument();
  });

  it("sends search and filter parameters and paginates", async () => {
    const user = userEvent.setup();
    mockApi({ ...base(), "GET /risk/suppliers": (u) => jsonResponse(u.searchParams.get("offset") === "25" ? page([row({ supplier_id: 26 })], 40) : page([row()], 40)) });
    renderApp("/risk/suppliers");
    await screen.findByText("Synthetic Supplier Alpha");
    await user.selectOptions(screen.getByLabelText("Classification"), "HIGH");
    await waitFor(() => expect(calls.some((c) => c.path === "/risk/suppliers" && c.search.get("classification") === "HIGH")).toBe(true));
    await user.type(screen.getByLabelText("Search"), "alp");
    await waitFor(() => expect(calls.some((c) => c.search.get("q") === "alp")).toBe(true));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(calls.some((c) => c.search.get("offset") === "25")).toBe(true));
  });

  it("shows an empty state for no matches", async () => {
    mockApi({ ...base(), "GET /risk/suppliers": () => jsonResponse(page([])) });
    renderApp("/risk/suppliers");
    expect(await screen.findByText("No suppliers match these filters")).toBeInTheDocument();
  });

  it("enables comparison only with two selections", async () => {
    const user = userEvent.setup();
    mockApi({ ...base(), "GET /risk/suppliers": () => jsonResponse(page([row(), row({ supplier_id: 2, name: "Synthetic Supplier Bravo" })])) });
    renderApp("/risk/suppliers");
    const link = await screen.findByRole("link", { name: /Compare selected \(0\)/ });
    expect(link).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByLabelText("Select Synthetic Supplier Alpha for comparison"));
    await user.click(screen.getByLabelText("Select Synthetic Supplier Bravo for comparison"));
    expect(screen.getByRole("link", { name: /Compare selected \(2\)/ })).toHaveAttribute("href", "/risk/compare?ids=1,2");
  });
});

describe("supplier detail", () => {
  it("shows score, coverage and reliability separately with evidence and unavailable factors", async () => {
    const user = userEvent.setup();
    mockApi({
      ...base(),
      "GET /risk/suppliers/1": () => jsonResponse(supplierRisk(assessment())),
      "GET /risk/suppliers/1/dependencies": () => jsonResponse({ supplier_id: 1, nodes: [], dependent_products: 0, dependent_facilities: 0, sole_sourced_count: 0, own_relationships_by_status: {}, downstream_reach: { depth: 2, nodes_by_type: {}, edges: 0, truncated: false, reasons: [] }, treatment: {}, notes: [] }),
      "GET /risk/suppliers/1/assessments": () => jsonResponse({ items: [], total: 0, limit: 20, offset: 0, note: "Each row is immutable." }),
    });
    renderApp("/risk/suppliers/1");
    expect(await screen.findByRole("heading", { name: "Synthetic Supplier Alpha" })).toBeInTheDocument();
    expect(screen.getByText("Risk score")).toBeInTheDocument();
    expect(screen.getByText("Evidence coverage")).toBeInTheDocument();
    expect(screen.getByText("Evidence reliability")).toBeInTheDocument();
    expect(screen.getByLabelText("Risk score 33.1 out of 100")).toBeInTheDocument();
    expect(screen.getByText(/No financial data source|Unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Missing evidence is not low risk/)).toBeInTheDocument();
    expect(screen.getByText("No verified compliance data source.", { exact: false })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show evidence for Concentration risk" }));
    expect(await screen.findByText(/SUPPLIES_PRODUCT DEMO-PL-001/)).toBeInTheDocument();
    expect(screen.getByText(/Share is 50% of 2 suppliers/)).toBeInTheDocument();
    expect(await screen.findByText("Each row is immutable.")).toBeInTheDocument();
  });

  it("explains an unassessed supplier", async () => {
    mockApi({ ...base(), "GET /risk/suppliers/1": () => jsonResponse(supplierRisk(null)) });
    renderApp("/risk/suppliers/1");
    expect(await screen.findByText("This supplier has not been assessed yet.")).toBeInTheDocument();
    expect(screen.queryByText("Risk score")).not.toBeInTheDocument();
  });

  it("shows not found for an unknown supplier", async () => {
    mockApi({ ...base(), "GET /risk/suppliers/99": () => jsonResponse({ error: { code: "not_found", message: "Supplier not found." } }, 404) });
    renderApp("/risk/suppliers/99");
    expect(await screen.findByRole("alert")).toHaveTextContent("Supplier not found.");
  });

  it("shows an override next to the unchanged automated result", async () => {
    const ov = { id: 5, supplier_id: 1, assessment_id: 7, action: "OVERRIDE" as const, factor_category: null, actor: "rita", actor_role: "reviewer", created_at: "2026-10-22T11:00:00Z", note: "Contract transition is planned.", override_classification: "HIGH" as const, override_expires_at: null, target_review_id: null, original_status: "ASSESSED", original_classification: "MODERATE" as const, original_score: 33.12, expired: false, stale: false, effective: true, note_on_staleness: null };
    mockApi({
      ...base(),
      "GET /risk/suppliers/1": () => jsonResponse(supplierRisk(assessment({ review_state: "OVERRIDDEN" }), { override: ov, reviews: [ov] })),
      "GET /risk/suppliers/1/dependencies": () => jsonResponse({ supplier_id: 1, nodes: [], dependent_products: 0, dependent_facilities: 0, sole_sourced_count: 0, own_relationships_by_status: {}, downstream_reach: { depth: 2, nodes_by_type: {}, edges: 0, truncated: false, reasons: [] }, treatment: {}, notes: [] }),
      "GET /risk/suppliers/1/assessments": () => jsonResponse({ items: [], total: 0, limit: 20, offset: 0, note: "n" }),
    });
    renderApp("/risk/suppliers/1");
    const note = await screen.findByText(/Analyst override:/);
    expect(note.closest("[role=alert]")).toHaveTextContent("rita");
    expect(note.closest("[role=alert]")).toHaveTextContent("The automated result (Moderate, 33.1) is unchanged");
  });

  const detailRoutes = (role: string, extra: Record<string, Handler> = {}, writes = true) => ({
    ...base(role, writes),
    "GET /risk/suppliers/1": () => jsonResponse(supplierRisk(assessment())),
    "GET /risk/suppliers/1/dependencies": () => jsonResponse({ supplier_id: 1, nodes: [], dependent_products: 0, dependent_facilities: 0, sole_sourced_count: 0, own_relationships_by_status: {}, downstream_reach: { depth: 2, nodes_by_type: {}, edges: 0, truncated: false, reasons: [] }, treatment: {}, notes: [] }),
    "GET /risk/suppliers/1/assessments": () => jsonResponse({ items: [], total: 0, limit: 20, offset: 0, note: "n" }),
    ...extra,
  });

  it("lets an editor acknowledge but not override", async () => {
    const user = userEvent.setup();
    mockApi(detailRoutes("editor", { "POST /risk/suppliers/1/reviews": () => jsonResponse({ id: 1 }, 201) }));
    renderApp("/risk/suppliers/1");
    const region = await screen.findByRole("region", { name: "Analyst review" });
    expect(within(region).getByRole("button", { name: "Override classification" })).toBeDisabled();
    await user.click(within(region).getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/risk/suppliers/1/reviews")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ assessment_id: 7, action: "ACKNOWLEDGE" });
    expect(await within(region).findByText("Acknowledge recorded.")).toBeInTheDocument();
  });

  it("requires a long enough note for an override and sends the chosen class", async () => {
    const user = userEvent.setup();
    mockApi(detailRoutes("reviewer", { "POST /risk/suppliers/1/reviews": () => jsonResponse({ id: 2 }, 201) }));
    renderApp("/risk/suppliers/1");
    const region = await screen.findByRole("region", { name: "Analyst review" });
    const btn = within(region).getByRole("button", { name: "Override classification" });
    expect(btn).toBeDisabled();
    await user.type(within(region).getByLabelText(/Note/), "Supplier moves to a dual-source contract next quarter.");
    expect(btn).toBeEnabled();
    await user.click(btn);
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ action: "OVERRIDE", override_classification: "HIGH" }));
  });

  it("disables every review action for a viewer and when writes are off", async () => {
    mockApi(detailRoutes("viewer"));
    const { unmount } = renderApp("/risk/suppliers/1");
    const region = await screen.findByRole("region", { name: "Analyst review" });
    for (const b of within(region).getAllByRole("button")) expect(b).toBeDisabled();
    unmount();
    mockApi(detailRoutes("admin", {}, false));
    renderApp("/risk/suppliers/1");
    const r2 = await screen.findByRole("region", { name: "Analyst review" });
    expect(within(r2).getByText(/Changes are disabled/)).toBeInTheDocument();
    expect(within(r2).getByRole("button", { name: "Acknowledge" })).toBeDisabled();
  });

  it("surfaces a stale-assessment conflict", async () => {
    const user = userEvent.setup();
    mockApi(detailRoutes("editor", { "POST /risk/suppliers/1/reviews": () => jsonResponse({ error: { code: "conflict", message: "That assessment was superseded." } }, 409) }));
    renderApp("/risk/suppliers/1");
    const region = await screen.findByRole("region", { name: "Analyst review" });
    await user.click(within(region).getByRole("button", { name: "Acknowledge" }));
    expect(await within(region).findByText("That assessment was superseded.")).toBeInTheDocument();
  });
});

describe("comparison", () => {
  const cmp = (over: Partial<Comparison> = {}): Comparison => ({
    comparable: true, incomparable_reasons: [], caveats: [],
    suppliers: [
      { supplier_id: 1, name: "Alpha", supplier_code: "A", assessment_id: 1, status: "ASSESSED", risk_score: 33, classification: "MODERATE", evidence_coverage: 0.65, reliability: 0.2, reliability_band: "LOW", methodology_version: "rm-1", computed_at: "2026-10-22T10:00:00Z" },
      { supplier_id: 2, name: "Bravo", supplier_code: "B", assessment_id: 2, status: "PARTIAL", risk_score: 50, classification: null, evidence_coverage: 0.33, reliability: 0.2, reliability_band: "LOW", methodology_version: "rm-1", computed_at: "2026-10-22T10:00:00Z" },
    ],
    matrix: [{ category: "FINANCIAL", label: "Financial risk", role: "risk", cells: [{ supplier_id: 1, state: "UNAVAILABLE", score: null, confidence: null, basis: "UNKNOWN" }, { supplier_id: 2, state: "UNAVAILABLE", score: null, confidence: null, basis: "UNKNOWN" }] }],
    ranking: [2, 1], ranking_note: null, ...over,
  });
  it("asks for a selection when fewer than two ids are given", async () => {
    mockApi({ ...base() });
    renderApp("/risk/compare?ids=1");
    expect(await screen.findByText("Choose two to six suppliers to compare")).toBeInTheDocument();
  });
  it("shows coverage and unavailable categories next to scores", async () => {
    mockApi({ ...base(), "GET /risk/compare": () => jsonResponse(cmp()) });
    renderApp("/risk/compare?ids=1,2");
    expect(await screen.findByRole("link", { name: "Alpha" })).toHaveAttribute("href", "/risk/suppliers/1");
    expect(calls.find((c) => c.path === "/risk/compare")?.search.getAll("ids")).toEqual(["1", "2"]);
    expect(screen.getAllByText("Unavailable").length).toBe(2);
    expect(screen.getByText(/Order by score/)).toHaveTextContent("Bravo › Alpha");
  });
  it("warns and drops the ranking when assessments are not comparable", async () => {
    mockApi({ ...base(), "GET /risk/compare": () => jsonResponse(cmp({ comparable: false, incomparable_reasons: ["Different methodology versions."], ranking: [] })) });
    renderApp("/risk/compare?ids=1,2");
    expect(await screen.findByText(/not directly comparable/)).toBeInTheDocument();
    expect(screen.getByText("Different methodology versions.")).toBeInTheDocument();
    expect(screen.queryByText(/Order by score/)).not.toBeInTheDocument();
  });
});

describe("review queue and runs", () => {
  it("states that alerts are in-app only and transitions with the version", async () => {
    const user = userEvent.setup();
    mockApi({
      ...base("editor"),
      "GET /risk/alerts": () => jsonResponse({ items: [alert()], total: 1, limit: 25, offset: 0 }),
      "POST /risk/alerts/3/transition": () => jsonResponse(alert({ status: "ACKNOWLEDGED", version: 2 })),
    });
    renderApp("/risk/alerts");
    expect(await screen.findByText(/does not send email, chat or push/)).toBeInTheDocument();
    expect(await screen.findByText("Now the only confirmed supplier of a node.")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/risk/alerts")?.search.get("status")).toBe("ACTIVE");
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ to: "ACKNOWLEDGED", version: 1 }));
  });
  it("disables transitions for a viewer and shows an empty queue", async () => {
    mockApi({ ...base("viewer"), "GET /risk/alerts": () => jsonResponse({ items: [alert()], total: 1, limit: 25, offset: 0 }) });
    const { unmount } = renderApp("/risk/alerts");
    expect(await screen.findByRole("button", { name: "Acknowledge" })).toBeDisabled();
    unmount();
    mockApi({ ...base(), "GET /risk/alerts": () => jsonResponse({ items: [], total: 0, limit: 25, offset: 0 }) });
    renderApp("/risk/alerts");
    expect(await screen.findByText("Nothing to review")).toBeInTheDocument();
  });
  it("lists runs with failures and gates the network run on the reviewer role", async () => {
    const user = userEvent.setup();
    mockApi({
      ...base("editor"),
      "GET /risk/runs": () => jsonResponse({ items: [run()], total: 1, limit: 20, offset: 0 }),
    });
    const { unmount } = renderApp("/risk/runs");
    expect(await screen.findByText("Supplier 9: boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run network assessment" })).toBeDisabled();
    unmount();
    mockApi({
      ...base("reviewer"),
      "GET /risk/runs": () => jsonResponse({ items: [run()], total: 1, limit: 20, offset: 0 }),
      "POST /risk/runs": () => jsonResponse(run({ id: 5, status: "QUEUED" }), 202),
    });
    renderApp("/risk/runs");
    await user.click(await screen.findByRole("button", { name: "Run network assessment" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ scope: "NETWORK" }));
    expect(await screen.findByText(/Run #5 queued/)).toBeInTheDocument();
  });
});

describe("methodology page", () => {
  it("states the method is a rule-based indicator and lists categories without data", async () => {
    mockApi({
      ...base(),
      "GET /risk/methodology": () =>
        jsonResponse({
          version: "rm-1", hash: "abcdef0123456789", kind: "deterministic rule-based indicator (not a trained or validated predictive model)", formula: "score = sum(W x s) / sum(W)",
          thresholds: { moderate: 30, high: 55, critical: 75 }, min_coverage_for_score: 0.3, min_coverage_for_classification: 0.5, min_scored_factors: 2, max_possible_coverage: 0.65,
          parameters_editable_via_api: false, disclaimer: interp,
          categories: [{ category: "FINANCIAL", label: "Financial risk", role: "risk", weight: 0.1, data_available: false, data_dependency: "A financial-data connector.", required_inputs: [], method: "Not implemented.", evidence_requirements: "n/a", freshness_policy: "n/a", uncertainty_treatment: "n/a", unavailable_when: "Always.", limitations: ["No source."], correlation_group: null }],
        }),
    });
    renderApp("/risk/methodology");
    expect(await screen.findByText(/not a trained or validated predictive model/)).toBeInTheDocument();
    expect(screen.getByText(/no data source/)).toBeInTheDocument();
    expect(screen.getByText("Needs: A financial-data connector.")).toBeInTheDocument();
    expect(screen.getByText(/cannot be edited through the API/)).toBeInTheDocument();
  });
});
