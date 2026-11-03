import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { edgeSubject } from "../lib/qualityApi";
import type { Aggregate, EntityQuality, Finding, Overview, RunInfo } from "../lib/qualityTypes";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; search: URLSearchParams; body?: unknown }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/v1/, "");
    calls.push({ method, path, search: url.searchParams, body: init?.body ? JSON.parse(String(init.body)) : undefined });
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
const agg = (over: Partial<Aggregate> = {}): Aggregate => ({
  subjects: 10, scored: 8, insufficient_evidence: 1, not_evaluated: 1, coverage: 0.8, mean: 87.5, min: 40, distribution: null, state: "SCORED", ...over,
});
const counts = (over = {}) => ({
  total: 12, open: 9, by_status: {}, open_by_severity: {}, open_by_dimension: { validity: 5, completeness: 2 }, open_by_entity_type: {}, critical_or_high_open: 3,
  open_requires_review: 1, accepted_exceptions: 2, ...over,
});
const overview = (over: Partial<Overview> = {}): Overview => ({
  has_evaluation: true, latest_run: null, latest_network_run: null,
  scores: { overall: agg(), by_subject_type: { SUPPLIER: agg() } }, findings: counts(), relationships_with_issues: 4,
  batches_with_warnings: [{ id: 7, batch_uid: "B-7", file_name: "suppliers.csv", status: "COMPLETED_WITH_ERRORS", total_rows: 10, rejected: 2, findings: 5 }],
  totals: {}, disclaimer: "Quality scores summarise the checks that exist. A high score does not prove that a record is factually correct.", empty_message: null, ...over,
});
const finding = (over: Partial<Finding> = {}): Finding => ({
  id: 1, origin: "EVALUATION", rule_id: "QR115", rule_name: "coordinates_not_placeholder", rule_version: 1, dimension: "validity", severity: "ERROR", status: "OPEN",
  entity_type: "FACILITY", entity_id: 5, entity_name: "Zero Site", external_id: null, field: "latitude", message: "Coordinates (0, 0) are a placeholder, not a site.",
  suggestion: null, evidence: { observed: "0,0" }, requires_review: false, batch_id: null, row_index: null, source_id: null, source_namespace: null, run_id: 1,
  first_detected_at: "2026-10-20T10:00:00Z", last_evaluated_at: "2026-10-21T10:00:00Z", resolved_at: null, resolved_by: null, resolution_note: null,
  assigned_to: null, reopened_count: 0, version: 3, fix_action: null, ...over,
});
const page = (items: Finding[], total = items.length) => ({ items, total, limit: 25, offset: 0 });
const detail = (f: Finding) => ({
  finding: f,
  rule: { rule_id: f.rule_id, name: "x", version: 1, description: "Coordinates are not the placeholder.", remediation: "Clear the coordinates if unknown.", fix_action: f.fix_action },
  events: [{ id: 1, occurred_at: "2026-10-20T10:00:00Z", actor: "system:evaluation", action: "DETECTED", from_status: null, to_status: "OPEN", note: null }],
});
const run = (over: Partial<RunInfo> = {}): RunInfo => ({
  id: 1, run_uid: "QEV-1", scope_type: "NETWORK", scope_ref: {}, status: "COMPLETED", trigger: "API", requested_by: "tester", queued_at: "2026-10-21T10:00:00Z",
  started_at: null, finished_at: null, duration_ms: 120, examined: { SUPPLIER: 3 }, checks_evaluated: 30, findings: { created: 2, resolved: 0, reopened: 0 },
  subjects_failed: 0, subjects_skipped: 0, truncated: false, truncation_reasons: [], error: null, ...over,
});
const emptyAnalytics = {
  trend: [], stale_distribution: { fresh_days: 90, stale_days: 365, by_entity_type: {} }, time_to_resolve_hours: { n: 0, median: null, mean: null },
  repeated_failures: { top_rules_by_open_findings: [] }, relationship_quality_distribution: agg({ subjects: 0, scored: 0, mean: null, coverage: null, state: "UNAVAILABLE" }), definitions: {},
};
const base = (role = "admin", writes = true): Record<string, Handler> => ({
  "GET /registry/me": me(role, writes),
  "GET /quality/analytics": () => jsonResponse(emptyAnalytics),
  "GET /quality/sources": () => jsonResponse({ items: [] }),
});

describe("data quality overview", () => {
  it("shows an empty state before any evaluation", async () => {
    mockApi({ ...base(), "GET /quality/overview": () => jsonResponse(overview({ has_evaluation: false, scores: null, findings: counts({ open: 0, total: 0, critical_or_high_open: 0, accepted_exceptions: 0, open_requires_review: 0, open_by_dimension: {} }), batches_with_warnings: [], empty_message: "No evaluation has been run yet." })) });
    renderApp("/quality");
    expect(await screen.findByText("No evaluation has been run yet")).toBeInTheDocument();
    expect(screen.getByText("Network quality score").parentElement).toHaveTextContent("—");
    expect(screen.getByText("Not evaluated")).toBeInTheDocument();
  });

  it("shows score with coverage, the disclaimer and import issues", async () => {
    mockApi({ ...base(), "GET /quality/overview": () => jsonResponse(overview()) });
    renderApp("/quality");
    expect((await screen.findAllByText("87.5")).length).toBeGreaterThan(0);
    expect(screen.getByText(/mean of 8 scored of 10 records \(80% coverage\)/)).toBeInTheDocument();
    expect(screen.getByText(/does not prove that a record is factually correct/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "suppliers.csv" })).toHaveAttribute("href", "/quality/batches/7");
    expect(screen.getByRole("link", { name: "Validity" })).toHaveAttribute("href", "/quality/findings?dimension=validity");
  });

  it("shows a permission error instead of data when access is denied", async () => {
    mockApi({ ...base("viewer"), "GET /quality/overview": () => jsonResponse({ error: { code: "forbidden", message: "Not allowed." } }, 403) });
    renderApp("/quality");
    expect(await screen.findByRole("alert")).toHaveTextContent("Not allowed.");
  });

  it("adds a navigation entry", async () => {
    mockApi({ ...base(), "GET /quality/overview": () => jsonResponse(overview()) });
    renderApp("/quality");
    expect(await screen.findByRole("link", { name: "Data quality" })).toHaveAttribute("href", "/quality");
  });
});

describe("findings workspace", () => {
  it("defaults to open findings, sends filters and paginates", async () => {
    const user = userEvent.setup();
    mockApi({ ...base(), "GET /quality/findings": (u) => jsonResponse(u.searchParams.get("offset") === "25" ? page([finding({ id: 26 })], 40) : page([finding()], 40)) });
    renderApp("/quality/findings");
    expect(await screen.findByText(/placeholder, not a site/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/quality/findings")?.search.get("status")).toBe("OPEN_ANY");
    await user.selectOptions(screen.getByLabelText("Minimum severity"), "ERROR");
    await waitFor(() => expect(calls.some((c) => c.search.get("min_severity") === "ERROR")).toBe(true));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(calls.some((c) => c.search.get("offset") === "25")).toBe(true));
    await user.selectOptions(screen.getByLabelText("Status"), "ALL");
    await waitFor(() => expect(calls.at(-1)?.search.has("status")).toBe(false));
  });

  it("shows an empty state with guidance", async () => {
    mockApi({ ...base(), "GET /quality/findings": () => jsonResponse(page([])) });
    renderApp("/quality/findings");
    expect(await screen.findByText("No findings match these filters")).toBeInTheDocument();
  });

  it("requires a note and the right role for review actions, and sends the version", async () => {
    const user = userEvent.setup();
    const f = finding();
    mockApi({
      ...base("reviewer"),
      "GET /quality/findings": () => jsonResponse(page([f])),
      "GET /quality/findings/1": () => jsonResponse(detail(f)),
      "POST /quality/findings/1/transition": () => jsonResponse({ ...f, status: "ACCEPTED_EXCEPTION", version: 4 }),
    });
    renderApp("/quality/findings");
    await user.click(await screen.findByRole("button", { name: /placeholder, not a site/ }));
    const panel = await screen.findByRole("region", { name: "Finding details" });
    const accept = within(panel).getByRole("button", { name: "Accept as exception" });
    expect(accept).toBeDisabled();
    await user.type(within(panel).getByLabelText(/Note/), "Known placeholder, survey pending.");
    expect(accept).toBeEnabled();
    await user.click(accept);
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ action: "accept_exception", expected_version: 3, note: "Known placeholder, survey pending." });
  });

  it("disables review actions for viewers and says why", async () => {
    const user = userEvent.setup();
    const f = finding();
    mockApi({ ...base("viewer"), "GET /quality/findings": () => jsonResponse(page([f])), "GET /quality/findings/1": () => jsonResponse(detail(f)) });
    renderApp("/quality/findings");
    await user.click(await screen.findByRole("button", { name: /placeholder, not a site/ }));
    const panel = await screen.findByRole("region", { name: "Finding details" });
    expect(within(panel).getByRole("button", { name: "Acknowledge" })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Mark resolved" })).toBeDisabled();
  });

  it("reports a version conflict and reloads the finding", async () => {
    const user = userEvent.setup();
    const f = finding();
    mockApi({
      ...base("editor"),
      "GET /quality/findings": () => jsonResponse(page([f])),
      "GET /quality/findings/1": () => jsonResponse(detail(f)),
      "POST /quality/findings/1/transition": () => jsonResponse({ error: { code: "version_conflict", message: "The finding changed (current version 4)." } }, 409),
    });
    renderApp("/quality/findings");
    await user.click(await screen.findByRole("button", { name: /placeholder, not a site/ }));
    const panel = await screen.findByRole("region", { name: "Finding details" });
    await user.click(within(panel).getByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByText(/The finding changed/)).toBeInTheDocument();
    await waitFor(() => expect(calls.filter((c) => c.path === "/quality/findings/1").length).toBeGreaterThan(1));
  });

  it("applies a safe fix only after explicit confirmation", async () => {
    const user = userEvent.setup();
    const f = finding({ fix_action: "CLEAR_PLACEHOLDER_COORDINATES" });
    mockApi({
      ...base("reviewer"),
      "GET /quality/findings": () => jsonResponse(page([f])),
      "GET /quality/findings/1": () => jsonResponse(detail(f)),
      "POST /quality/findings/1/remediate": () => jsonResponse({ status: "APPLIED", fields: ["latitude", "longitude"] }),
    });
    renderApp("/quality/findings");
    await user.click(await screen.findByRole("button", { name: /placeholder, not a site/ }));
    const panel = await screen.findByRole("region", { name: "Finding details" });
    const apply = within(panel).getByRole("button", { name: "Apply fix" });
    expect(apply).toBeDisabled();
    await user.click(within(panel).getByRole("checkbox"));
    await user.click(apply);
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/remediate"))).toBe(true));
    expect(calls.find((c) => c.path.endsWith("/remediate"))?.body).toEqual({ expected_version: 3, confirm: true });
  });
});

const entity = (over: Partial<EntityQuality> = {}): EntityQuality => ({
  entity: { type: "SUPPLIER", id: 2, name: "Acme Parts", status: "ACTIVE" }, score: { state: "NOT_EVALUATED", score: null },
  open_findings: [], other_findings: [], history: [], source_records: [], related_relationships: [], ...over,
});

describe("entity quality detail", () => {
  it("distinguishes not evaluated from a zero score", async () => {
    mockApi({ ...base(), "GET /quality/entities/SUPPLIER/2": () => jsonResponse(entity()) });
    renderApp("/quality/entity/SUPPLIER/2");
    expect(await screen.findByText("No evaluation has covered this record yet.")).toBeInTheDocument();
    expect(screen.queryByText("0.0")).not.toBeInTheDocument();
  });

  it("explains insufficient evidence and shows relationship quality with proposed status", async () => {
    mockApi({
      ...base(),
      "GET /quality/entities/SUPPLIER/2": () =>
        jsonResponse(entity({
          score: { state: "INSUFFICIENT_EVIDENCE", score: null },
          related_relationships: [{ ref: "SUPPLY:3", entity_type: "RELATIONSHIP", entity_id: 3, relationship_type: "SUPPLIES", status: "PROPOSED", score: { state: "SCORED", score: 95 }, open_findings: 0 }],
        })),
    });
    renderApp("/quality/entity/SUPPLIER/2");
    expect(await screen.findByText(/Fewer than three applicable checks/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "SUPPLY:3" })).toHaveAttribute("href", "/quality/entity/RELATIONSHIP/3");
    expect(screen.getByText(/A proposed relationship is not an invalid one/)).toBeInTheDocument();
  });

  it("loads provenance on demand and states its limits", async () => {
    const user = userEvent.setup();
    mockApi({
      ...base(),
      "GET /quality/entities/SUPPLIER/2": () => jsonResponse(entity()),
      "GET /quality/lineage/SUPPLIER/2": () =>
        jsonResponse({ layers_present: ["ingestion_lineage"], has_source_record: true, limitations: ["The audit hash chain is not proof against someone with database write access."], ingestion: [{ batch_id: 7, external_id: "S-1", row_index: 1, mapping_profile_ref: "builtin:supplier", record_fingerprint: "abcdef1234567890", outcome: "CREATED", changed_fields: ["name"], ingested_at: "2026-10-20T10:00:00Z" }], audit: [], merges: [], edge_events: [] }),
    });
    renderApp("/quality/entity/SUPPLIER/2");
    await user.click(await screen.findByRole("button", { name: "Show provenance" }));
    expect(await screen.findByText(/not proof against someone/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Batch 7" })).toHaveAttribute("href", "/quality/batches/7");
  });

  it("rejects an unknown record reference", async () => {
    mockApi(base());
    renderApp("/quality/entity/WIDGET/abc");
    expect(await screen.findByRole("alert")).toHaveTextContent("Unknown record reference");
  });
});

describe("import quality report", () => {
  const report = (redacted: boolean) => ({
    batch: { id: 7, batch_uid: "B-7", file_name: "suppliers.csv", status: "COMPLETED_WITH_ERRORS", entity_type: "SUPPLIER", policy: "PARTIAL", total_rows: 3, source_namespace: "erp-a", failure_reason: null },
    counts: { IMPORTED: 1, IMPORTED_WITH_WARNINGS: 0, REQUIRES_REVIEW: 0, REJECTED: 1, DUPLICATE_SKIPPED: 1, FAILED: 0, NOT_APPLIED: 0 },
    all_rows_accounted_for: true, file_level_findings: [], records_evaluated: agg(),
    rows: { items: [{ row_index: 2, source_line: 3, external_id: "S-2", outcome: "REJECTED", classification: "REJECTED", message: null, entity_id: null, findings: [{ id: 9, rule_id: "DQ002", severity: "ERROR", field: "name", message: "Name is required.", suggestion: null, observed_value: redacted ? null : "(blank)", observed_value_redacted: redacted }] }], total: 1, limit: 50, offset: 0 },
    classification_rules: "Rules text.", raw_payload: "Raw source rows are not stored.",
  });

  it("shows every outcome class, filters by class and hides values for viewers", async () => {
    const user = userEvent.setup();
    mockApi({ ...base("viewer"), "GET /quality/batches/7": () => jsonResponse(report(true)) });
    renderApp("/quality/batches/7");
    expect(await screen.findByRole("button", { name: "Rejected (1)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Duplicate Skipped (1)" })).toBeInTheDocument();
    expect(screen.getByText(/value hidden for your role/)).toBeInTheDocument();
    expect(screen.getByText(/Raw source rows are not stored/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Rejected (1)" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/quality/batches/7" && c.search.get("classification") === "REJECTED")).toBe(true));
  });

  it("shows observed values to roles allowed to see them", async () => {
    mockApi({ ...base("editor"), "GET /quality/batches/7": () => jsonResponse(report(false)) });
    renderApp("/quality/batches/7");
    expect(await screen.findByText(/value: \(blank\)/)).toBeInTheDocument();
  });
});

describe("evaluation runs", () => {
  it("starts a run, shows partial completion and polls while active", async () => {
    const user = userEvent.setup();
    let polls = 0;
    mockApi({
      ...base("reviewer"),
      "GET /quality/runs": () => {
        polls += 1;
        return jsonResponse({ items: [run({ id: 2, status: polls < 3 ? "RUNNING" : "PARTIAL", truncated: true, truncation_reasons: ["subject limit reached"] }), run()], total: 2, limit: 15, offset: 0 });
      },
      "POST /quality/runs": () => jsonResponse(run({ id: 3, status: "QUEUED" }), 202),
    });
    renderApp("/quality/runs");
    expect(await screen.findByText("Running")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start evaluation" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ scope: "NETWORK" });
    expect(await screen.findByText(/Partial: subject limit reached/, undefined, { timeout: 6000 })).toBeInTheDocument();
  }, 10000);

  it("disables starting a network run for editors", async () => {
    mockApi({ ...base("editor"), "GET /quality/runs": () => jsonResponse({ items: [], total: 0, limit: 15, offset: 0 }) });
    renderApp("/quality/runs");
    expect(await screen.findByText("No evaluation runs yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start evaluation" })).toBeDisabled();
  });
});

describe("rules and network explorer integration", () => {
  it("lets only administrators change a rule and requires a reason", async () => {
    const user = userEvent.setup();
    const rule = { rule_id: "QR141", name: "record_fresh", version: 1, description: "Fresh.", dimension: "freshness", severity: "WARNING", entity_types: ["SUPPLIER"], enabled: true, default_enabled: true, requires_review: false, remediation: "Update.", fix_action: null, override: null };
    mockApi({
      ...base("admin"),
      "GET /quality/rules": () => jsonResponse({ items: [rule], formula_version: "qs-1" }),
      "PATCH /quality/rules/QR141": () => jsonResponse({ rule_id: "QR141", enabled: false, note: "Takes effect at the next evaluation." }),
    });
    renderApp("/quality/rules");
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    const confirm = screen.getByRole("button", { name: "Confirm" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("Reason"), "too noisy");
    await user.click(confirm);
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ enabled: false, reason: "too noisy" });
  });

  it("maps relationship references to quality subjects", () => {
    expect(edgeSubject("SUPPLY:3")).toEqual({ kind: "RELATIONSHIP", id: 3 });
    expect(edgeSubject("PRODUCT:9")).toEqual({ kind: "PRODUCT_LINK", id: 9 });
    expect(edgeSubject("OWNER:1")).toBeUndefined();
    expect(edgeSubject("SUPPLY:x")).toBeUndefined();
  });
});
