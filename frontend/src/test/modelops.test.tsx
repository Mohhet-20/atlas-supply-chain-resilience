import { render, screen, within } from "@testing-library/react";
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
const page = <T,>(items: T[], total = items.length) => ({ items, total, limit: 15, offset: 0 });
const mv = (over: Record<string, unknown> = {}) => ({
  model_uid: "MV-AAAA1111", model_key: "leadtime/lead_time/ridge_leadtime", version: "LT-0001", family: "lead-time", task: "REGRESSION", domain: "LEAD_TIME",
  target: "lead time (CALENDAR_DAYS)", lifecycle_state: "EVALUATED", state_changed_at: "2026-10-30T10:00:00Z", source_type: "LEADTIME_RUN", source_ref: "LT-0001",
  algorithm: "ridge_leadtime", artifact_ref: "leadtime_run:LT-0001", created_by: "tester", created_at: "2026-10-30T09:00:00Z", ...over,
});
const detail = (over: Record<string, unknown> = {}) => ({
  ...mv(), intended_use: ["Estimate lead time for planning"], prohibited_use: ["Contractual commitments"], limitations: ["Learned from this scope only"], operating_conditions: {},
  feature_schema: null, preprocessing_version: "lt-1", data_snapshot_hash: "abc", training_window: null, evaluation_window: null, hyperparameters: {}, seed: 1,
  artifact_checksum: "deadbeef", code_revision: "unknown", dependency_versions: { python: "3.10" }, fallback_behavior: null, known_issues: [], monitoring_requirements: [],
  statement: "A registered version is a record of a model that exists; registration is not an approval.", ...over,
});
const evalSummary = (over: Record<string, unknown> = {}) => ({
  eval_uid: "EV-1", kind: "RECORDED_BACKTEST", status: "COMPLETED", result_status: "completed", requested_by: "tester", algorithm_version: "mo-1", config: {}, config_hash: "h",
  queued_at: "2026-10-30T10:00:00Z", finished_at: "2026-10-30T10:00:01Z", duration_ms: 12, error: null, validity_overall: "valid_with_unchecked", ...over,
});
const evalDetail = (over: Record<string, unknown> = {}) => ({
  ...evalSummary(), dataset_ref: {}, split: {}, sample: { test: 40 },
  metrics: { mae: { value: 1.234, n: 40, status: "ok", note: null }, wape: { value: null, n: 40, status: "undefined", note: "undefined: actuals sum to zero" } },
  baselines: { entries: { global_median: { metrics: { mae: { value: 2.5, n: 40, status: "ok", note: null } } } }, note: "Candidates and baselines were scored on the same chronological test split." },
  folds: [], segments: null, calibration: null, intervals: null,
  validity: { overall: "valid_with_unchecked", checks: [{ check: "window_order", status: "pass", detail: "training < validation < test in time" }, { check: "fit_boundaries", status: "not_checked", detail: "the run does not declare what each step was fitted on" }] },
  warnings: [], limitations: [], reproducibility: { config_hash: "h" }, software: {}, statements: ["RECORDED_BACKTEST evidence is what the training run measured; it is re-expressed, not re-fitted."], ...over,
});

describe("registry", () => {
  it("shows an empty state", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /modelops/models": () => jsonResponse(page([])) });
    renderApp("/models");
    expect(await screen.findByText("No model versions registered")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(/does not mean the model is approved/i);
    expect(screen.getByText(/Registering needs the editor role/)).toBeInTheDocument();
  });

  it("lists real versions, filters through the API, and registers runs for editors", async () => {
    const spy = mockApi({
      "GET /registry/me": me("editor"),
      "GET /modelops/models": () => jsonResponse(page([mv(), mv({ model_uid: "MV-BBBB2222", model_key: "risk/rule_based", domain: "SUPPLIER_RISK", task: "RULE_BASED_INDICATOR", lifecycle_state: "DEVELOPMENT", version: "rm-1" })])),
      "POST /modelops/models/sync": () => jsonResponse({ registered: 2, already_registered: 1, not_registrable: 0 }),
    });
    renderApp("/models");
    expect(await screen.findByRole("link", { name: "risk/rule_based" })).toBeInTheDocument();
    expect(screen.getByText("development")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Domain"), "LEAD_TIME");
    expect(calls.some((c) => c.path === "/modelops/models" && c.search.get("domain") === "LEAD_TIME")).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Register finished runs" }));
    expect(await screen.findByRole("status")).toHaveTextContent("2 new model version(s) registered");
    spy.mockRestore();
  });

  it("shows an error with retry", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /modelops/models": () => jsonResponse({ error: { code: "boom", message: "Server trouble" } }, 500) });
    renderApp("/models");
    expect(await screen.findByText("Server trouble")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("comparison", () => {
  it("warns that incompatible models are not comparable and never shows a missing metric as zero", async () => {
    mockApi({
      "GET /registry/me": me("viewer"),
      "GET /modelops/compare": () => jsonResponse({
        comparable: false, note: "n", warnings: ["The models differ in task (CLASSIFICATION, REGRESSION); their metrics measure different things and must not be ranked against each other."],
        items: [{ ...mv(), evaluation: { eval_uid: "EV-1", kind: "RECORDED_BACKTEST", validity: "valid", sample: {}, metrics: { mae: { value: 1.5, n: 40, status: "ok", note: null } } } }, { ...mv({ model_uid: "MV-BBBB2222", model_key: "other", task: "CLASSIFICATION" }), evaluation: null }],
      }),
    });
    renderApp("/models/compare?ids=MV-AAAA1111,MV-BBBB2222");
    expect(await screen.findByRole("alert")).toHaveTextContent(/must not be ranked/);
    expect(screen.getByText("no completed evaluation")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("asks for a selection when too few models are chosen", async () => {
    mockApi({ "GET /registry/me": me("viewer") });
    renderApp("/models/compare?ids=MV-AAAA1111");
    expect(await screen.findByText("Choose models to compare")).toBeInTheDocument();
  });
});

describe("model detail", () => {
  const base = {
    "GET /registry/me": me("reviewer"), "GET /modelops/models/MV-AAAA1111": () => jsonResponse(detail()),
  };
  it("shows intended use, prohibited use, limitations and traceability", async () => {
    mockApi(base);
    renderApp("/models/MV-AAAA1111");
    expect(await screen.findByText("Contractual commitments")).toBeInTheDocument();
    expect(screen.getByText("Learned from this scope only")).toBeInTheDocument();
    expect(screen.getByText("leadtime_run:LT-0001")).toBeInTheDocument();
  });

  it("shows metrics with sample sizes, undefined values as words, baselines and unchecked validity", async () => {
    mockApi({ ...base, "GET /modelops/models/MV-AAAA1111/evaluations": () => jsonResponse(page([evalSummary()])), "GET /modelops/evaluations/EV-1": () => jsonResponse(evalDetail()) });
    renderApp("/models/MV-AAAA1111");
    await userEvent.click(await screen.findByRole("tab", { name: "Evaluation" }));
    expect(await screen.findByText(/1\.234/)).toBeInTheDocument();
    expect(screen.getAllByText(/n=40/).length).toBeGreaterThan(0);
    expect(screen.getByText(/undefined — undefined: actuals sum to zero/)).toBeInTheDocument();
    expect(screen.getByText(/same chronological test split/)).toBeInTheDocument();
    expect(screen.getByText(/\? not checked/)).toBeInTheDocument();
  });

  it("shows unsupported evaluations as unsupported, not as a score", async () => {
    mockApi({ ...base, "GET /modelops/models/MV-AAAA1111/evaluations": () => jsonResponse(page([evalSummary({ result_status: "unsupported" })])), "GET /modelops/evaluations/EV-1": () => jsonResponse(evalDetail({ result_status: "unsupported", metrics: null, baselines: null, warnings: ["Predictive evaluation is not available: ATLAS holds no outcome labels."] })) });
    renderApp("/models/MV-AAAA1111");
    await userEvent.click(await screen.findByRole("tab", { name: "Evaluation" }));
    expect(await screen.findByText("Evaluation not supported")).toBeInTheDocument();
    expect(screen.getByText(/no outcome labels/)).toBeInTheDocument();
  });

  it("shows a not-evaluated empty state", async () => {
    mockApi({ ...base, "GET /modelops/models/MV-AAAA1111/evaluations": () => jsonResponse(page([])) });
    renderApp("/models/MV-AAAA1111");
    await userEvent.click(await screen.findByRole("tab", { name: "Evaluation" }));
    expect(await screen.findByText("Not evaluated yet")).toBeInTheDocument();
  });

  it("queues an evaluation for editors through the API", async () => {
    mockApi({ ...base, "GET /modelops/models/MV-AAAA1111/evaluations": () => jsonResponse(page([])), "POST /modelops/models/MV-AAAA1111/evaluations": () => jsonResponse(evalSummary({ status: "QUEUED", result_status: null }), 202) });
    renderApp("/models/MV-AAAA1111");
    await userEvent.click(await screen.findByRole("tab", { name: "Evaluation" }));
    await userEvent.click(await screen.findByRole("button", { name: "Evaluate recorded backtest" }));
    expect(await screen.findByText("Evaluation EV-1 queued.")).toBeInTheDocument();
    expect(JSON.parse(calls.find((c) => c.method === "POST")?.text ?? "{}")).toEqual({ kind: "RECORDED_BACKTEST" });
  });

  it("explains: unsupported SHAP is stated, not faked", async () => {
    mockApi({ ...base, "POST /modelops/models/MV-AAAA1111/explanations": (_u, init) => {
      const b = JSON.parse(String(init?.body));
      return jsonResponse(b.method === "shap" ? { status: "unsupported", method: "shap", reason: "SHAP values are not computed in ATLAS.", model_version: "MV-AAAA1111", statement: "x" } : { status: "ok", kind: "learned_linear", method: "stored_global", model_version: "MV-AAAA1111", statement: "x", coefficients: [{ feature: "quantity", beta: 0.4 }] });
    } });
    renderApp("/models/MV-AAAA1111");
    await userEvent.click(await screen.findByRole("tab", { name: "Explainability" }));
    expect(screen.getByRole("note")).toHaveTextContent(/not evidence that anything caused/);
    await userEvent.click(screen.getByRole("button", { name: "Try SHAP" }));
    expect(await screen.findByText("SHAP values are not computed in ATLAS.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Global explanation" }));
    expect(await screen.findByText(/Traceable to model version MV-AAAA1111/)).toBeInTheDocument();
  });

  it("governance: reviewers see decisions, refusals are shown, history lists evidence", async () => {
    mockApi({
      ...base,
      "GET /modelops/models/MV-AAAA1111/lifecycle": () => jsonResponse({ model_uid: "MV-AAAA1111", state: "EVALUATED", allowed_transitions: [], statement: "A registered version is a record.", items: [{ from_state: null, to_state: "DEVELOPMENT", actor: "system", note: "registered", evidence: null, created_at: "2026-10-30T09:00:00Z" }, { from_state: "DEVELOPMENT", to_state: "EVALUATED", actor: "tester", note: "evaluation completed", evidence: { evaluation: "EV-1", validity: "valid" }, created_at: "2026-10-30T10:00:00Z" }] }),
      "POST /modelops/models/MV-AAAA1111/transition": () => jsonResponse({ error: { code: "domain_validation_error", message: "Approval needs a written rationale (at least 10 characters)." } }, 422),
    });
    renderApp("/models/MV-AAAA1111");
    await userEvent.click(await screen.findByRole("tab", { name: "Governance" }));
    const hist = await screen.findByRole("list", { name: "Lifecycle history" });
    expect(within(hist).getByText(/evidence: evaluation EV-1/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Move to approved" }));
    expect(await screen.findByRole("status")).toHaveTextContent(/written rationale/);
  });

  it("governance: viewers cannot decide", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /modelops/models/MV-AAAA1111": () => jsonResponse(detail()), "GET /modelops/models/MV-AAAA1111/lifecycle": () => jsonResponse({ model_uid: "MV-AAAA1111", state: "EVALUATED", allowed_transitions: [], statement: "s", items: [] }) });
    renderApp("/models/MV-AAAA1111");
    await userEvent.click(await screen.findByRole("tab", { name: "Governance" }));
    expect(await screen.findByText(/needs the reviewer role/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Move to/ })).not.toBeInTheDocument();
  });

  it("monitoring never equates drift with deterioration", async () => {
    mockApi({ ...base, "GET /modelops/models/MV-AAAA1111/monitoring": () => jsonResponse({ ...page([{ id: 1, check_uid: "MC-1", kind: "COVARIATE_DRIFT", feature: "supplier", status: "warning", severity: "warning", statistic: { value: 0.18 }, explanation: "1 category value not seen in the reference period", created_at: "2026-10-30T11:00:00Z" }]), statement: "Drift is a signal." }) });
    renderApp("/models/MV-AAAA1111");
    await userEvent.click(await screen.findByRole("tab", { name: "Monitoring" }));
    expect(await screen.findByText(/not seen in the reference period/)).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(/does not show that the model has become inaccurate/);
  });

  it("model card shows missing evidence explicitly", async () => {
    mockApi({ ...base, "GET /modelops/models/MV-AAAA1111/card": () => jsonResponse({
      statement: "A model card is documentation generated from stored records.", identity: { model_key: "k" }, intended_use: ["Plan"], prohibited_use: ["Commit"], limitations: "not recorded",
      training: {}, reproducibility: { artifact_ref: "leadtime_run:LT-0001" }, evaluation: { recorded_backtest: "not recorded", prospective: "not recorded", missing: ["RECORDED_BACKTEST", "PROSPECTIVE"] },
      monitoring: { requirements: [], open_signals: [], note: "n" }, governance: { state: "DEVELOPMENT", history: [] }, statements: {},
    }) });
    renderApp("/models/MV-AAAA1111");
    await userEvent.click(await screen.findByRole("tab", { name: "Model card" }));
    expect(await screen.findByText(/Missing evidence: recorded backtest, prospective/)).toBeInTheDocument();
    expect(screen.getByText(/No open drift or data-quality signals are recorded/)).toBeInTheDocument();
  });
});

describe("capabilities", () => {
  it("lists what is not supported", async () => {
    mockApi({ "GET /registry/me": me("viewer"), "GET /modelops/health": () => jsonResponse({ algorithm_version: "mo-1", write_api_enabled: true, registered_versions: 0, by_state: {}, not_supported: ["automatic retraining", "SHAP, LIME and counterfactual explanations"], statements: { tenancy: "Data is separated by organisation." }, explanation_methods: { supported: [], unsupported: {} }, metric_definitions: { mae: { definition: "mean(|p − a|)", direction: "lower is better" } } }) });
    renderApp("/models/about");
    expect(await screen.findByText("automatic retraining")).toBeInTheDocument();
    expect(screen.getByText(/separated by organisation/)).toBeInTheDocument();
    expect(screen.getByText("mae")).toBeInTheDocument();
  });
});
