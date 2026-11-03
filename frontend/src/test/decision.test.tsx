import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

// All data below is SYNTHETIC test data.
type Handler = (url: URL, init?: RequestInit) => Response;
let calls: { method: string; path: string; text?: string }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/v1/, "");
    calls.push({ method, path, text: typeof init?.body === "string" ? init.body : undefined });
    const h = routes[`${method} ${path}`];
    return Promise.resolve(h ? h(url, init) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
  });
}
const renderApp = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
beforeEach(() => tokenStore.clear());
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const me = (role: string, writes = true) => () => jsonResponse({ actor: "tester", role, authenticated: true, auth_required: true, writes_enabled: writes });

const health = { algorithm_version: "dc-1", metric_registry_version: "mr-1", write_api_enabled: true, cases_by_status: { DRAFT: 1 }, limits: { max_evidence: 12 }, statuses: [], transitions: {}, evidence_kinds: [], not_supported: ["executing a decision: approval changes nothing operational"], statements: { decision_support: "Approval records a decision; it executes nothing." } };
const metrics = { registry_version: "mr-1", metrics: [{ id: "MODELLED_COST", name: "Modelled cost", category: "Cost", unit: "currency", direction: "min", time_basis: "one period", definition: "Known cost", missing: "unknown", cost_sensitive: true, scope_dependent: false }], unavailable: [{ id: "RECOVERY_TIME", name: "Recovery time", reason: "No recovery model exists." }] };

const caseItem = (over: Record<string, unknown> = {}) => ({
  case_uid: "DC-AAAA1111", title: "Dual sourcing study", status: "EVALUATED", version: 1, created_by: "other", reviewer: null, cloned_from: null, superseded_by: null,
  current_evaluation_uid: "EV-1111", selected_alternative: null, created_at: "2026-11-01T06:00:00Z", updated_at: "2026-11-01T06:00:00Z", allowed_transitions: ["DRAFT", "UNDER_REVIEW", "EVALUATED"], ...over,
});
const detail = (over: Record<string, unknown> = {}) => ({
  ...caseItem(), definition: { title: "Dual sourcing study", purpose: "Compare plans under an outage", scenario_ids: [], evidence: [], probabilities: [], assumptions: [] },
  approval: { state: "PROPOSED", executed: false }, evaluation_count: 1, event_count: 2, ...over,
});
const row = (metric: string, b: number | null, v: number | null, verdict: string, status = "comparable", extra: Record<string, unknown> = {}) => ({
  metric, condition: "NORMAL", reference_condition: "NORMAL", baseline: b, value: v, unit: "units", direction: "min", difference: b === null || v === null ? null : v - b, relative_pct: b ? ((v ?? 0) - b) / b * 100 : null,
  relative_note: null, verdict, status, reason: status === "incomparable" ? "currency differ (USD vs EUR)" : null, ...extra,
});
const counts = { better: 1, worse: 1, same: 0, changed: 0, comparable: 2, incomparable: 0, missing: 0 };
const result = (over: Record<string, unknown> = {}) => ({
  result_status: "completed_with_warnings", result_hash: "a".repeat(64), algorithm_version: "dc-1", metric_registry_version: "mr-1",
  baseline: { key: "RR-BASE:A", label: "Current plan", scope: {}, reference_kind: null }, conditions: [],
  comparisons: [{ plan_key: "RR-OTHER:B", label: "Dual source plan", role: "ALTERNATIVE", scope_differences: [], counts, classification: "TRADE_OFF", conditions: [{ condition: "NORMAL", reference_condition: "NORMAL", reference_note: null, counts, classification: "TRADE_OFF", rows: [row("MODELLED_COST", 100, 120, "worse"), row("UNMET_UNITS", 10, 2, "better"), row("CONCENTRATION_HHI", 0.5, 0.4, "unknown", "incomparable")] }] }],
  aggregation: [{ plan_key: "RR-OTHER:B", weighted: false, weighting_reason: "at least one disrupted condition has no probability", probability_basis: null, metrics: [{ metric: "UNMET_UNITS", unit: "units", n_conditions: 1, of_conditions: 1, min: { value: 10, condition: "out [FIXED]" }, max: { value: 10, condition: "out [FIXED]" }, worst: { value: 10, condition: "out [FIXED]" }, best: { value: 10, condition: "out [FIXED]" }, expected: null, expected_reason: "no probability" }] }],
  alternatives: [
    { key: "MAINTAIN_BASELINE", label: "Maintain the baseline plan", source: "RR-BASE", classification: "BASELINE", improves: [], worsens: [], scenarios_improved: [], scenarios_worsened: [], constraints: { all_passed: true, failed: [], checked: 1 }, feasible: true, prerequisites: [], uncertainties: [], suitability: "Keeps the reference plan.", cost_notes: [] },
    { key: "RR-OTHER:B", label: "Dual source plan", source: "RR-OTHER", classification: "TRADE_OFF", improves: ["Unmet units"], worsens: ["Modelled cost"], scenarios_improved: ["out [FIXED]"], scenarios_worsened: [], constraints: { all_passed: true, failed: [], checked: 1 }, feasible: true, prerequisites: ["Approving records a decision only."], uncertainties: ["Not comparable because one side does not state them: Concentration."], suitability: "Improves unmet units; worsens cost.", cost_notes: ["Modelled cost changes by +20.00 USD (+20.0%)."] },
  ],
  exposure: [], warnings: [{ code: "RUN_HAD_WARNINGS", text: "RR-OTHER completed with warnings." }], issues: [], unavailable_metrics: [{ id: "RECOVERY_TIME", name: "Recovery time", reason: "No recovery model exists." }],
  statements: { tradeoff: "A response that is better on some metrics and worse on others is a trade-off, not a winner." }, ...over,
});
const evaluation = (over: Record<string, unknown> = {}, res = result()) => ({
  evaluation_uid: "EV-1111", case_version: 1, result_status: res.result_status, algorithm_version: "dc-1", metric_registry_version: "mr-1", result_hash: "a".repeat(64), snapshot_hash: "b".repeat(64),
  evaluated_by: "other", evaluated_at: "2026-11-01T06:05:00Z", duration_ms: 5, alternatives: [], warning_count: 1, result: res, ...over,
});
const caseRoutes = (role: string, d = detail(), ev = evaluation()) => ({
  "GET /registry/me": me(role), "GET /decision/cases/DC-AAAA1111": () => jsonResponse(d), "GET /decision/cases/DC-AAAA1111/evaluations/EV-1111": () => jsonResponse(ev),
  "GET /decision/cases/DC-AAAA1111/evaluations": () => jsonResponse({ items: [], total: 1, limit: 50, offset: 0 }),
  "GET /decision/cases/DC-AAAA1111/events": () => jsonResponse({ items: [{ id: 1, kind: "CREATED", from_status: null, to_status: "DRAFT", actor: "other", at: "2026-11-01T06:00:00Z", text: null, evaluation_uid: null, alternative_key: null, data: null }], total: 1, limit: 50, offset: 0 }),
});

describe("decision center", () => {
  it("lists cases and shows the empty state", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /decision/health": () => jsonResponse(health), "GET /decision/cases": () => jsonResponse({ items: [], total: 0, limit: 15, offset: 0 }) });
    renderApp("/decision");
    expect(await screen.findByText("No decision cases yet")).toBeInTheDocument();
    expect(screen.getByText(/executes nothing/)).toBeInTheDocument();
    cleanup();
    mockApi({ "GET /registry/me": me("viewer"), "GET /decision/health": () => jsonResponse(health), "GET /decision/cases": () => jsonResponse({ items: [{ ...caseItem(), current_result_status: "completed", alternative_count: 2 }], total: 1, limit: 15, offset: 0 }) });
    renderApp("/decision");
    expect(await screen.findByRole("link", { name: "DC-AAAA1111" })).toBeInTheDocument();
    expect(screen.getByText("Dual sourcing study")).toBeInTheDocument();
  });

  it("shows an error state with retry", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /decision/health": () => jsonResponse(health), "GET /decision/cases": () => jsonResponse({ error: { code: "boom", message: "Server broke" } }, 500) });
    renderApp("/decision");
    expect(await screen.findByText(/Server broke/)).toBeInTheDocument();
  });

  it("shows a trade-off with words, never a winner, and states what cannot be compared", async () => {
    mockApi(caseRoutes("editor"));
    renderApp("/decision/cases/DC-AAAA1111");
    expect((await screen.findAllByText(/Trade-off: better on some metrics/)).length).toBeGreaterThan(0);
    expect(screen.getAllByText("worse").length).toBeGreaterThan(0);
    expect(screen.getAllByText("better").length).toBeGreaterThan(0);
    expect(screen.getByText(/currency differ/)).toBeInTheDocument();
    expect(screen.getByText(/no defensible probabilities/)).toBeInTheDocument();
    expect(screen.getByText(/no time series|No attached evidence reports a time series/)).toBeInTheDocument();
    expect(screen.getByText(/No scenario simulation run was attached/)).toBeInTheDocument();
    expect(screen.getByText(/Recovery time/)).toBeInTheDocument();
    expect(screen.queryByText(/winner:|best overall/i)).toBeNull();
    expect(screen.getByText(/Maintain the baseline/, { selector: "h4" })).toBeInTheDocument();
  });

  it("says hidden for a viewer instead of showing zero", async () => {
    const hidden = result();
    const rows = hidden.comparisons[0]!.conditions[0]!.rows;
    rows[0] = row("MODELLED_COST", null, null, "worse", "comparable", { hidden: true, difference: null, relative_pct: null });
    mockApi(caseRoutes("viewer", detail(), evaluation({ cost_redacted: true }, hidden)));
    renderApp("/decision/cases/DC-AAAA1111");
    expect((await screen.findAllByText("hidden for your role")).length).toBeGreaterThan(0);
    expect(screen.getByText(/cost figures are hidden for your role/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Re-evaluate" })).toBeNull();
  });

  it("shows an insufficient-data state with the reasons", async () => {
    const res = result({ result_status: "insufficient_data", comparisons: [], alternatives: [], issues: [{ code: "BASELINE_REQUIRED", text: "No plan is designated BASELINE." }] });
    mockApi(caseRoutes("editor", detail(), evaluation({ result_status: "insufficient_data" }, res)));
    renderApp("/decision/cases/DC-AAAA1111");
    expect(await screen.findByRole("alert")).toHaveTextContent("not enough data");
    expect(screen.getByText(/No plan is designated BASELINE/)).toBeInTheDocument();
  });

  it("shows the unevaluated state and lets an editor evaluate", async () => {
    const user = userEvent.setup();
    const d = detail({ status: "DRAFT", current_evaluation_uid: null, allowed_transitions: ["READY", "EVALUATED"], evaluation_count: 0 });
    mockApi({ ...caseRoutes("editor", d), "POST /decision/cases/DC-AAAA1111/evaluate": () => jsonResponse({ case: d, evaluation: evaluation(), reused: true }) });
    renderApp("/decision/cases/DC-AAAA1111");
    expect(await screen.findByText("Not evaluated yet")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Evaluate" }));
    expect(await screen.findByText(/existing evaluation was reused/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/evaluate"))).toBe(true);
  });

  it("lets a reviewer approve a named alternative with a rationale, and says nothing was executed", async () => {
    const user = userEvent.setup();
    const d = detail({ status: "UNDER_REVIEW", reviewer: "tester", allowed_transitions: ["EVALUATED", "APPROVED", "REJECTED"] });
    mockApi({ ...caseRoutes("reviewer", d), "POST /decision/cases/DC-AAAA1111/decision": () => jsonResponse({ ...d, status: "APPROVED" }) });
    renderApp("/decision/cases/DC-AAAA1111");
    const approve = await screen.findByRole("button", { name: "Approve alternative" });
    expect(approve).toBeDisabled();
    await screen.findByRole("option", { name: "Maintain the baseline" });
    await user.selectOptions(screen.getByLabelText("Alternative to approve"), "MAINTAIN_BASELINE");
    await user.type(screen.getByLabelText(/Rationale/), "Cost increase is not justified");
    await user.click(approve);
    expect(await screen.findByText(/Nothing was executed/)).toBeInTheDocument();
    const post = calls.find((c) => c.path.endsWith("/decision"));
    expect(JSON.parse(post!.text!)).toEqual({ decision: "APPROVED", rationale: "Cost increase is not justified", alternative_key: "MAINTAIN_BASELINE" });
  });

  it("hides review controls from editors and when writes are disabled", async () => {
    const d = detail({ status: "UNDER_REVIEW", reviewer: "tester" });
    mockApi(caseRoutes("editor", d));
    renderApp("/decision/cases/DC-AAAA1111");
    await screen.findByText("Human review");
    expect(screen.queryByRole("button", { name: "Approve alternative" })).toBeNull();
    cleanup();
    mockApi({ ...caseRoutes("admin", d), "GET /registry/me": me("admin", false) });
    renderApp("/decision/cases/DC-AAAA1111");
    await screen.findByText("Human review");
    expect(screen.queryByRole("button", { name: "Approve alternative" })).toBeNull();
  });

  it("builds a case: needs a baseline run, validates, and creates a draft", async () => {
    const user = userEvent.setup();
    const cands = { items: [{ kind: "RESILIENCE_RUN", run_uid: "RR-BASE", result_status: "completed", recommended_id: "A", alternatives: ["A"], has_reference_plan: false, finished_at: null }, { kind: "RESILIENCE_RUN", run_uid: "RR-OTHER", result_status: "completed", recommended_id: "B", alternatives: ["B"], has_reference_plan: false, finished_at: null }], note: "" };
    mockApi({
      "GET /registry/me": me("editor"), "GET /decision/evidence/candidates": () => jsonResponse(cands),
      "POST /decision/cases/validate": () => jsonResponse({ ok: false, issues: [{ code: "BASELINE_REQUIRED", text: "Exactly one evidence run must have the role BASELINE." }], evidence: [] }),
      "POST /decision/cases": () => jsonResponse(detail({ status: "DRAFT" }), 201), "GET /decision/cases/DC-AAAA1111": () => jsonResponse(detail({ status: "DRAFT", current_evaluation_uid: null })),
      "GET /decision/cases/DC-AAAA1111/evaluations": () => jsonResponse({ items: [], total: 0, limit: 50, offset: 0 }), "GET /decision/cases/DC-AAAA1111/events": () => jsonResponse({ items: [], total: 0, limit: 50, offset: 0 }),
    });
    renderApp("/decision/new");
    const create = await screen.findByRole("button", { name: "Create draft case" });
    expect(create).toBeDisabled();
    await user.type(screen.getByLabelText(/^Title/), "Dual sourcing study");
    await user.type(screen.getByLabelText(/^Purpose/), "Compare plans");
    await user.selectOptions(screen.getByLabelText("Role of RR-BASE"), "ALTERNATIVE");
    await user.click(screen.getByRole("button", { name: "Check definition" }));
    expect(await screen.findByText(/Exactly one evidence run must have the role BASELINE/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Role of RR-BASE"), "BASELINE");
    await user.selectOptions(screen.getByLabelText("Role of RR-OTHER"), "ALTERNATIVE");
    await user.click(screen.getByRole("button", { name: "Create draft case" }));
    expect(await screen.findByText("Human review")).toBeInTheDocument();
    const body = JSON.parse(calls.find((c) => c.method === "POST" && c.path === "/decision/cases")!.text!);
    expect(body.evidence).toEqual([{ kind: "RESILIENCE_RUN", run_uid: "RR-BASE", role: "BASELINE" }, { kind: "RESILIENCE_RUN", run_uid: "RR-OTHER", role: "ALTERNATIVE" }]);
  });

  it("shows metric definitions and what is not available", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /decision/health": () => jsonResponse(health), "GET /decision/metrics": () => jsonResponse(metrics) });
    renderApp("/decision/about");
    const t = await screen.findByRole("table", { name: "Metric registry" });
    expect(within(t).getByText("Modelled cost")).toBeInTheDocument();
    expect(screen.getByText(/No recovery model exists/)).toBeInTheDocument();
    expect(screen.getByText(/approval changes nothing operational/)).toBeInTheDocument();
  });
});
