import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { tokenStore } from "../lib/registryApi";
import type { Candidate, CandidateComparison, MergePreview, Me, Overview, SearchHit } from "../lib/registryTypes";
import { jsonResponse } from "./fixtures";

type Handler = (req: { url: URL; init?: RequestInit }) => Response;
let calls: { method: string; path: string; headers: Record<string, string>; body: unknown; search: string }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/v1/, "");
    calls.push({
      method,
      path,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      search: url.search,
    });
    const h = routes[`${method} ${path}`];
    return Promise.resolve(h ? h({ url, init }) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
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

const me = (role: Me["role"], extra: Partial<Me> = {}): Me => ({ actor: `${role}-user`, role, authenticated: true, auth_required: true, writes_enabled: true, ...extra });
const meRoute = (role: Me["role"], extra: Partial<Me> = {}): Handler => () => jsonResponse(me(role, extra));

const overview: Overview = {
  counts: {
    SUPPLIER: { total: 12, live: 11, merged: 1 },
    FACILITY: { total: 5, live: 5, merged: 0 },
    PRODUCT: { total: 4, live: 4, merged: 0 },
  },
  candidates: { by_status: { NEW: 3 }, awaiting_review: 3, open_by_tier: { STRONG: 1, POSSIBLE: 2 }, confirmed_duplicates: 0 },
  merges: { executed: 1, compensated: 0 },
  quality: { has_data: false, average_overall: null, records_with_missing_required_fields: null, low_score_count: null, trend: [], message: "No quality assessment has been run yet." },
  recent_changes: [{ id: 1, occurred_at: "2026-10-19T10:00:00Z", actor: "editor-user", action: "registry.create", target_type: "SUPPLIER", target_id: "7" }],
};

const hit: SearchHit = {
  type: "SUPPLIER", id: 7, code: "USR-SUP-1", name: "Acme <b>Components</b> GmbH", status: "ACTIVE", country_or_region: "Germany", category: "Electronics",
  merged_into: null, source_namespace: null, external_id: null, updated_at: "2026-10-19T10:00:00Z", version: 1,
  match_reasons: [{ kind: "EXACT_NAME", field: "name", exact: true, detail: "Acme" }], exact: true, approximate: false, best_reason: "EXACT_NAME",
};

const evidence = {
  rule_version: "rc-1", score: 0.55, score_meaning: "similarity score from summed evidence points; not a probability", tier: "POSSIBLE" as const, hard_conflict: false,
  families: ["name", "location"], thresholds: { strong: 0.8, possible: 0.55 },
  signals: [{ signal: "name_exact", family: "name", points: 0.4, detail: "normalized names are identical" }],
};
const candidate = (over: Partial<Candidate> = {}): Candidate => ({
  id: 3, entity_type: "SUPPLIER", left_id: 7, right_id: 8, left_name: "Acme Components GmbH", right_name: "ACME Components G.m.b.H.", rule_version: "rc-1",
  score: 0.55, tier: "POSSIBLE", hard_conflict: false, status: "NEW", evidence, left_version: 1, right_version: 1, stale: false,
  first_seen_at: "2026-10-19T10:00:00Z", updated_at: "2026-10-19T10:00:00Z", ...over,
});
const side = (id: number) => ({
  id, code: `USR-SUP-${id}`, version: 1, lifecycle: "ACTIVE", provenance: "USER_PROVIDED", source_namespace: null, external_id: null,
  created_at: "2026-10-19T10:00:00Z", identifiers: [], aliases: [], relationships: {}, lineage_count: 0,
});
const comparison = (c: Candidate): CandidateComparison => ({
  candidate: c,
  fields: [{ field: "name", left: "Acme Components GmbH", right: "ACME Components G.m.b.H.", left_normalized: "acme components gmbh", right_normalized: "acme components gmbh", equal: false, conflict: true, missing_side: null }],
  left: side(7), right: side(8), history: [],
});

describe("registry overview", () => {
  it("renders counts from the API and an empty quality state", async () => {
    mockApi({ "GET /registry/me": meRoute("viewer"), "GET /registry/overview": () => jsonResponse(overview) });
    renderApp("/registry");
    expect(await screen.findByText("Awaiting review")).toBeInTheDocument();
    expect(screen.getByText("1 merged away · 12 total")).toBeInTheDocument();
    expect(screen.getByText("No quality assessment has been run yet.")).toBeInTheDocument();
    expect(screen.getByText(/Scores are evidence-based similarity values, not probabilities/)).toBeInTheDocument();
  });

  it("shows a clear error and retry when unauthorised", async () => {
    mockApi({
      "GET /registry/me": () => jsonResponse({ error: { code: "unauthenticated", message: "Authentication required." } }, 401),
      "GET /registry/overview": () => jsonResponse({ error: { code: "unauthenticated", message: "Authentication required." } }, 401),
    });
    renderApp("/registry");
    expect((await screen.findAllByText("Authentication required.")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("bearer token", () => {
  it("sends the token only in the Authorization header, never in the URL, and can clear it", async () => {
    const user = userEvent.setup();
    mockApi({ "GET /registry/me": meRoute("reviewer"), "GET /registry/overview": () => jsonResponse(overview) });
    renderApp("/registry");
    await screen.findByText("Awaiting review");
    await user.type(screen.getByLabelText("API token"), "tok-1234567890-abcdef");
    await user.click(screen.getByRole("button", { name: "Use token" }));
    await waitFor(() => expect(calls.some((c) => c.headers.Authorization === "Bearer tok-1234567890-abcdef")).toBe(true));
    expect(calls.every((c) => !c.search.includes("tok-1234567890"))).toBe(true);
    expect(screen.getByLabelText("API token")).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(tokenStore.get()).toBe("");
  });
});

describe("registry explorer", () => {
  it("labels exact vs approximate results and escapes names", async () => {
    const user = userEvent.setup();
    const approx: SearchHit = { ...hit, id: 9, name: "Acme Comp", exact: false, approximate: true, best_reason: "FUZZY", match_reasons: [{ kind: "FUZZY", field: "name", exact: false, detail: "Acme Comp", score: 0.86 }] };
    mockApi({
      "GET /registry/me": meRoute("viewer"),
      "GET /registry/search": () => jsonResponse({ items: [hit, approx], total: 2, limit: 25, offset: 0, truncated: false, notes: ["Approximate matches are suggestions only; they never imply the records are the same entity."] }),
    });
    renderApp("/registry/explorer");
    await user.type(await screen.findByLabelText(/Search by name/), "acme");
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Exact")).toBeInTheDocument();
    expect(within(table).getByText("Approximate")).toBeInTheDocument();
    expect(within(table).getByText(/86% similar/)).toBeInTheDocument();
    expect(within(table).getByText("Acme <b>Components</b> GmbH")).toBeInTheDocument();
    expect(table.querySelector("b")).toBeNull();
    expect(screen.getByText(/never imply the records are the same entity/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New record" })).not.toBeInTheDocument();
  });

  it("offers record creation only to editors", async () => {
    mockApi({ "GET /registry/me": meRoute("editor") });
    renderApp("/registry/explorer");
    expect(await screen.findByRole("button", { name: "New record" })).toBeInTheDocument();
  });

  it("hides write controls when the server has writes disabled", async () => {
    mockApi({ "GET /registry/me": meRoute("admin", { writes_enabled: false }) });
    renderApp("/registry/explorer");
    await screen.findByText(/Search by name/);
    await waitFor(() => expect(screen.queryByRole("button", { name: "New record" })).not.toBeInTheDocument());
  });
});

describe("duplicate review", () => {
  const pageRoutes = (role: Me["role"], c: Candidate) => ({
    "GET /registry/me": meRoute(role),
    "GET /registry/candidates": () => jsonResponse({ items: [c], total: 1, limit: 20, offset: 0 }),
    [`GET /registry/candidates/${c.id}`]: () => jsonResponse(comparison(c)),
  });

  it("lists candidates with tier, score meaning and conflicts", async () => {
    mockApi(pageRoutes("reviewer", candidate({ hard_conflict: true, tier: "POSSIBLE", stale: true })));
    renderApp("/registry/duplicates");
    expect(await screen.findByText("Possible duplicate")).toBeInTheDocument();
    expect(screen.getByText("Conflicting authoritative identifiers")).toBeInTheDocument();
    expect(screen.getByText("Records changed since scoring")).toBeInTheDocument();
    expect(screen.getByText("0.55")).toHaveAttribute("title", expect.stringContaining("not a probability"));
  });

  it("viewer cannot trigger detection or decisions", async () => {
    mockApi(pageRoutes("viewer", candidate()));
    renderApp("/registry/duplicates/3");
    expect(await screen.findByText("Your decision")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Confirm duplicate" })).toBeDisabled());
    expect(screen.getAllByText(/needs the/).length).toBeGreaterThan(0);
  });

  it("shows side-by-side values, normalised forms and evidence", async () => {
    mockApi(pageRoutes("reviewer", candidate()));
    renderApp("/registry/duplicates/3");
    const cmp = await screen.findByRole("region", { name: "Side-by-side comparison" });
    expect(within(cmp).getByText("Acme Components GmbH", { selector: "td" })).toBeInTheDocument();
    expect(within(cmp).getAllByText(/normalised: acme components gmbh/).length).toBe(2);
    expect(screen.getByText("Name Exact")).toBeInTheDocument();
    expect(screen.getByText(/not a probability/)).toBeInTheDocument();
  });

  it("requires acknowledgement UI for identifier conflicts and posts the decision with the reason", async () => {
    const user = userEvent.setup();
    const c = candidate({ hard_conflict: true });
    mockApi({
      ...pageRoutes("reviewer", c),
      "POST /registry/candidates/3/decision": () => jsonResponse({ ...c, status: "CONFIRMED_DUPLICATE" }),
    });
    renderApp("/registry/duplicates/3");
    await screen.findByText("Your decision");
    expect(screen.getByText(/different authoritative identifiers/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Reason/), "verified by phone");
    await user.click(screen.getByLabelText(/conflicting authoritative identifiers/));
    await user.click(screen.getByRole("button", { name: "Confirm duplicate" }));
    await waitFor(() => expect(calls.some((x) => x.method === "POST" && x.path.endsWith("/decision"))).toBe(true));
    const post = calls.find((x) => x.method === "POST")!;
    expect(post.body).toEqual({ decision: "CONFIRMED_DUPLICATE", reason: "verified by phone", acknowledge_identifier_conflict: true });
  });

  it("surfaces server refusals", async () => {
    const user = userEvent.setup();
    mockApi({
      ...pageRoutes("reviewer", candidate()),
      "POST /registry/candidates/3/decision": () => jsonResponse({ error: { code: "invalid_transition", message: "Cannot move from NEW to MERGED." } }, 409),
    });
    renderApp("/registry/duplicates/3");
    await user.click(await screen.findByRole("button", { name: "Confirm duplicate" }));
    expect(await screen.findByText("Cannot move from NEW to MERGED.")).toBeInTheDocument();
  });

  it("offers merge entry points only for confirmed duplicates, with explicit survivor choice", async () => {
    mockApi(pageRoutes("admin", candidate({ status: "CONFIRMED_DUPLICATE" })));
    renderApp("/registry/duplicates/3");
    const link = await screen.findByRole("link", { name: /Keep “Acme Components GmbH”/ });
    expect(link).toHaveAttribute("href", "/registry/merge?type=SUPPLIER&survivor=7&merged=8&candidate=3");
    expect(screen.getByRole("link", { name: /Keep “ACME Components G.m.b.H.”/ })).toHaveAttribute("href", expect.stringContaining("survivor=8&merged=7"));
  });
});

describe("merge console", () => {
  const detail = (id: number, name: string) => ({
    type: "SUPPLIER", id, code: `USR-SUP-${id}`, lifecycle: "ACTIVE", version: 1, name, attributes: {}, normalized: { name: name.toLowerCase() }, source_namespace: null,
    aliases: [], identifiers: [], relationships: {}, lineage: [], identity: { merged_into: null, merged_from: [] }, quality: null, open_candidates: 0, history: [],
  });
  const preview = (over: Partial<MergePreview> = {}): MergePreview => ({
    entity_type: "SUPPLIER", survivor: { id: 7, code: "USR-SUP-7", name: "Acme", version: 1 }, merged: { id: 8, code: "USR-SUP-8", name: "ACME", version: 1 },
    fields: [{ field: "name", columns: ["name"], survivor: "Acme", merged: "ACME", status: "CONFLICT", default_choice: null, choice: null, resolved_value: null, needs_decision: true }],
    resulting_values: {}, relationship_impact: [{ table: "supply_relationships", row_id: 1, code: "REL-1", column: "supplier_id", old: 8, new: 7, action: "REASSIGN" }],
    identifiers: { preserved: [], conflicts: [] }, aliases_to_add: [], review: { candidate_id: 3, status: "CONFIRMED_DUPLICATE", matches_pair: true },
    blockers: [{ code: "unresolved_field_conflict", message: "Choose a value for 'name'." }], warnings: [], can_execute: false, ...over,
  });
  const base = (role: Me["role"], pv: MergePreview) => ({
    "GET /registry/me": meRoute(role),
    "GET /registry/suppliers/7": () => jsonResponse(detail(7, "Acme")),
    "GET /registry/suppliers/8": () => jsonResponse(detail(8, "ACME")),
    "POST /registry/merges/preview": () => jsonResponse(pv),
  });
  const URL_ = "/registry/merge?type=SUPPLIER&survivor=7&merged=8&candidate=3";

  it("blocks execution until a fresh, executable preview is confirmed", async () => {
    const user = userEvent.setup();
    mockApi({ ...base("admin", preview()), "POST /registry/merges": () => jsonResponse({ id: 1 }, 201) });
    renderApp(URL_);
    await user.click(await screen.findByRole("button", { name: "Preview merge" }));
    expect(await screen.findByText(/Cannot merge yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Execute merge" })).not.toBeInTheDocument();
    expect(screen.getByText(/Reassigned to survivor/)).toBeInTheDocument();
  });

  it("sends field decisions and requires reason + confirmation to execute", async () => {
    const user = userEvent.setup();
    const ok = preview({ blockers: [], can_execute: true });
    mockApi({
      ...base("admin", preview()),
      "POST /registry/merges/preview": () => jsonResponse(ok),
      "POST /registry/merges": () => jsonResponse({ id: 5, merge_uid: "M-5", entity_type: "SUPPLIER", survivor_id: 7, merged_id: 8, status: "EXECUTED", actor: "admin-user", authenticated: true, reason: "dup", candidate_id: 3, survivor_version_before: 1, survivor_version_after: 2, created_at: "2026-10-19T10:00:00Z", compensated_at: null, compensated_by: null, compensation_reason: null, reassigned_count: 1, dropped_count: 0, field_decisions: { rows: [] } }, 201),
      "GET /registry/merges/5": () => jsonResponse({ id: 5, merge_uid: "M-5", entity_type: "SUPPLIER", survivor_id: 7, merged_id: 8, status: "EXECUTED", actor: "admin-user", authenticated: true, reason: "dup", candidate_id: 3, survivor_version_before: 1, survivor_version_after: 2, created_at: "2026-10-19T10:00:00Z", compensated_at: null, compensated_by: null, compensation_reason: null, reassigned_count: 1, dropped_count: 0, field_decisions: { rows: [] } }),
    });
    renderApp(URL_);
    await user.click(await screen.findByRole("button", { name: "Preview merge" }));
    await user.click(await screen.findByLabelText("Keep survivor's value"));
    // changing a decision invalidates the preview
    expect(await screen.findByText(/Inputs changed/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh preview" }));
    const exec = await screen.findByRole("button", { name: "Execute merge" });
    expect(exec).toBeDisabled();
    await user.type(screen.getByLabelText(/Reason for the merge/), "same company");
    await user.click(screen.getByLabelText(/I have reviewed this preview/));
    expect(exec).toBeEnabled();
    await user.click(exec);
    expect(await screen.findByText("Merge #5")).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST" && c.path === "/registry/merges")!;
    expect(post.body).toMatchObject({
      entity_type: "SUPPLIER", survivor_id: 7, merged_id: 8, survivor_version: 1, merged_version: 1, candidate_id: 3,
      field_decisions: { name: { choice: "KEEP_SURVIVOR" } }, reason: "same company",
    });
  });

  it("reviewers can preview but not execute", async () => {
    const user = userEvent.setup();
    mockApi(base("reviewer", preview({ blockers: [], can_execute: true })));
    renderApp(URL_);
    await user.click(await screen.findByRole("button", { name: "Preview merge" }));
    await user.type(await screen.findByLabelText(/Reason for the merge/), "same company");
    await user.click(await screen.findByLabelText(/I have reviewed this preview/));
    expect(screen.getByRole("button", { name: "Execute merge" })).toBeDisabled();
    expect(screen.getByText(/needs the/)).toHaveTextContent("admin");
  });

  it("explains version conflicts and offers a reload", async () => {
    const user = userEvent.setup();
    mockApi({
      ...base("reviewer", preview()),
      "POST /registry/merges/preview": () => jsonResponse({ error: { code: "version_conflict", message: "The record was changed concurrently. Reload and try again." } }, 409),
    });
    renderApp(URL_);
    await user.click(await screen.findByRole("button", { name: "Preview merge" }));
    expect(await screen.findByText(/changed concurrently/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload records" })).toBeInTheDocument();
  });

  it("rejects an invalid selection without calling the API for records", async () => {
    mockApi({ "GET /registry/me": meRoute("admin") });
    renderApp("/registry/merge?type=SUPPLIER&survivor=7&merged=7");
    expect(await screen.findByText(/Choose a survivor and a merged record/)).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/registry/suppliers"))).toBe(false);
  });
});

describe("record detail", () => {
  const rec = (over: Record<string, unknown> = {}) => ({
    type: "SUPPLIER", id: 7, code: "USR-SUP-7", lifecycle: "ACTIVE", version: 3, name: "Acme", attributes: { name: "Acme", country_or_region: "Germany", provenance: "USER_PROVIDED", name_normalized: "acme", version: 3 },
    normalized: { name: "acme" }, source_namespace: null, aliases: [{ id: 1, alias: "Acme Old", alias_normalized: "acme old", kind: "FORMER_NAME", origin_merge_id: null, created_by: "x" }],
    identifiers: [{ id: 1, scheme: "DUNS", value: "555", source_id: 1, source_namespace: "erp", authoritative: true, primary: false, origin_merge_id: 2, created_by: "x" }],
    relationships: { supplies_facilities: [{ id: 1, code: "REL-1" }] }, lineage: [], identity: { merged_into: null, merged_from: [{ id: 9, code: "USR-SUP-9", name: "Acme Dup", merge_id: 4 }] },
    quality: null, open_candidates: 2, history: [{ id: 1, occurred_at: "2026-10-19T10:00:00Z", actor: "editor-user", authenticated: true, action: "registry.create", outcome: "SUCCESS", details: {} }], ...over,
  });

  it("shows identity, provenance of identifiers, lineage and history", async () => {
    mockApi({ "GET /registry/me": meRoute("viewer"), "GET /registry/suppliers/7": () => jsonResponse(rec()) });
    renderApp("/registry/suppliers/7");
    expect(await screen.findByRole("heading", { name: "Acme" })).toBeInTheDocument();
    expect(screen.getByText(/carried over from merge #2/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Acme Dup" })).toHaveAttribute("href", "/registry/suppliers/9");
    expect(screen.getByRole("link", { name: "merge #4" })).toBeInTheDocument();
    expect(screen.getByText(/2 possible duplicate candidates/)).toBeInTheDocument();
    expect(screen.getByText(/Not assessed yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("merged records are read-only and point to the survivor", async () => {
    mockApi({
      "GET /registry/me": meRoute("admin"),
      "GET /registry/suppliers/7": () => jsonResponse(rec({ lifecycle: "MERGED", identity: { merged_into: { id: 1, code: "USR-SUP-1", name: "Survivor" }, merged_from: [] } })),
    });
    renderApp("/registry/suppliers/7");
    expect(await screen.findByText(/was merged into/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Survivor" })).toHaveAttribute("href", "/registry/suppliers/1");
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("sends the expected version on edit and reports a conflict", async () => {
    const user = userEvent.setup();
    mockApi({
      "GET /registry/me": meRoute("editor"),
      "GET /registry/suppliers/7": () => jsonResponse(rec()),
      "PATCH /registry/suppliers/7": () => jsonResponse({ error: { code: "version_conflict", message: "The record was changed concurrently. Reload and try again." } }, 409),
    });
    renderApp("/registry/suppliers/7");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit record" });
    const country = within(form).getByLabelText("Country / region");
    await user.clear(country);
    await user.type(country, "France");
    await user.click(within(form).getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText(/changed concurrently/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ expected_version: 3, country_or_region: "France" });
    expect(within(form).getByRole("button", { name: "Reload latest" })).toBeInTheDocument();
  });

  it("rejects unknown record kinds", async () => {
    mockApi({ "GET /registry/me": meRoute("viewer") });
    renderApp("/registry/widgets/3");
    expect(await screen.findByText("Not found")).toBeInTheDocument();
  });
});

describe("quality console", () => {
  const summary = {
    has_data: true, run: { id: 2, run_uid: "QLT-1", config_version: "qc-1", config_hash: "a".repeat(64), actor: "editor-user", started_at: "2026-10-19T10:00:00Z", entities_assessed: 20, average_overall: 0.9, dimension_averages: { completeness: 0.8, validity: 1, uniqueness: 0.95 } },
    by_entity_type: { SUPPLIER: { count: 12, average: 0.88 } }, by_source: [], issue_counts: { MISSING: 3 }, low_score_count: 2, low_score_threshold: 0.6, trend: [], disclaimer: "A quality score is a diagnostic indicator, not a guarantee.",
  };
  const issues = { items: [{ dimension: "completeness", kind: "MISSING", severity: "ERROR", field: "category", message: "Required field 'category' is empty.", remediation: "Provide a value for category.", entity_type: "SUPPLIER", entity_id: 7, entity_score: 0.7, entity_name: "Acme" }], total: 1, limit: 20, offset: 0, has_data: true, run_id: 2 };

  it("shows dimensions, config version and actionable issues", async () => {
    mockApi({
      "GET /registry/me": meRoute("viewer"),
      "GET /registry/quality/summary": () => jsonResponse(summary),
      "GET /registry/quality/config": () => jsonResponse({ version: "qc-1", hash: "h", config: {}, dimensions: { completeness: "Weighted share of required fields." } }),
      "GET /registry/quality/issues": () => jsonResponse(issues),
    });
    renderApp("/registry/quality");
    expect(await screen.findByText("Quality dimensions")).toBeInTheDocument();
    expect(screen.getByText(/configuration qc-1/)).toBeInTheDocument();
    expect(screen.getByText("Weighted share of required fields.")).toBeInTheDocument();
    expect(await screen.findByText("Provide a value for category.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run quality assessment" })).toBeDisabled();
  });

  it("explains the no-data state", async () => {
    mockApi({
      "GET /registry/me": meRoute("editor"),
      "GET /registry/quality/summary": () => jsonResponse({ has_data: false, message: "No quality assessment has been run yet." }),
      "GET /registry/quality/config": () => jsonResponse({ version: "qc-1", hash: "h", config: {}, dimensions: {} }),
      "GET /registry/quality/issues": () => jsonResponse({ items: [], total: 0, limit: 20, offset: 0, has_data: false, run_id: null }),
    });
    renderApp("/registry/quality");
    expect(await screen.findByText("No quality assessment yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run quality assessment" })).toBeEnabled();
  });
});
