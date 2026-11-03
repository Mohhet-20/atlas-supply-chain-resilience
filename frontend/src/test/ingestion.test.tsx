import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { api, ApiError } from "../lib/api";
import { tokenStore } from "../lib/token";
import type { Batch, Finding, MappingProfile, PreviewResponse } from "../lib/ingestionTypes";
import { definitionFromRows, localProblems, parseValueMap, rowsFromDefinition } from "../lib/mapping";
import { jsonResponse, page } from "./fixtures";

afterEach(() => vi.restoreAllMocks());

type Handler = (req: { url: URL; init?: RequestInit }) => Response;
function mockApi(routes: Record<string, Handler>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const key = `${init?.method ?? "GET"} ${url.pathname.replace(/^\/api\/v1/, "")}`;
    const h = routes[key];
    return Promise.resolve(h ? h({ url, init }) : jsonResponse({ error: { code: "not_found", message: `no route ${key}` } }, 404));
  });
}
const renderApp = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );

const batch: Batch = {
  id: 7, batch_uid: "BATCH-ABC", status: "COMPLETED_WITH_ERRORS", policy: "PARTIAL", source_id: 1, source_namespace: "erp-a",
  entity_type: "SUPPLIER", source_format: "CSV", mapping_profile_ref: "canonical:1", mapping_profile_id: null, file_name: "s.csv",
  content_sha256: "a".repeat(64), content_bytes: 120, fingerprint: "b".repeat(64), normalization_version: "1", total_rows: 5,
  created_count: 3, updated_count: 0, unchanged_count: 0, rejected_count: 1, skipped_count: 0, duplicate_count: 1, finding_count: 2,
  failure_reason: null, started_at: "2026-10-19T10:00:00Z", finished_at: "2026-10-19T10:00:01Z",
};
const finding: Finding = {
  id: 11, finding_key: "k", rule_id: "DQ001", severity: "ERROR", batch_id: 7, source_id: 1, entity_type: "SUPPLIER", external_id: "S-4",
  field: "name", row_index: 4, source_line: 5, message: "Required field 'name' is empty <img src=x onerror=alert(1)>", suggestion: "Fill it in.",
  observed_value: null, resolution_status: "OPEN", resolution_note: null, resolved_at: null, created_at: "2026-10-19T10:00:00Z",
};
const summary = { sources: 2, profiles: 1, active_profiles: 1, batches: 3, batches_by_status: { COMMITTED: 3 }, open_findings: 4, open_findings_by_severity: { ERROR: 3, WARNING: 1 }, rows_processed: 120 };
const source = { id: 1, namespace: "erp-a", name: "ERP A", source_type: "CSV_FILE", provenance: "IMPORTED_EXTERNAL", status: "ACTIVE", description: null, created_at: "2026-10-19T10:00:00Z" };
const fields = (names: string[]) => names.map((n) => ({ name: n, kind: "text", required: n === "external_id" || n === "name", description: "" }));
const formats = {
  formats: [], entity_types: { SUPPLIER: fields(["external_id", "name", "country_or_region", "category"]), FACILITY: [], PRODUCT: [], RELATIONSHIP: [], PRODUCT_LINK: [] },
  limits: { max_upload_bytes: 1000, max_rows: 5000, max_json_depth: 32 }, encodings: ["utf-8"], delimiters: [",", ";", "tab", "pipe"], policies: ["ATOMIC", "PARTIAL"], normalization_version: "1",
};

describe("mapping helpers", () => {
  it("parses value maps and builds a minimal definition", () => {
    expect(parseValueMap("plant=MANUFACTURING\n bad line \nx = y ")).toEqual({ plant: "MANUFACTURING", x: "y" });
    const rows = rowsFromDefinition(fields(["external_id", "name", "category"]), undefined, { external_id: { source: "Id" }, name: { source: "Name" } });
    rows[1]!.transform = "trim, upper";
    rows[1]!.valueMap = "a=B";
    const def = definitionFromRows(rows, "reject", "%d/%m/%Y");
    expect(def.columns).toEqual({ external_id: { source: "Id" }, name: { source: "Name", transform: ["trim", "upper"], value_map: { a: "B" } } });
    expect(def.unknown_field_policy).toBe("reject");
    expect(def.date_format).toBe("%d/%m/%Y");
  });
  it("flags missing required and duplicate sources locally", () => {
    const rows = rowsFromDefinition(fields(["external_id", "name", "category"]), undefined, { external_id: { source: "A" }, category: { source: "a" } });
    expect(localProblems(rows).join(" ")).toMatch(/"name" is required/);
    expect(localProblems(rows).join(" ")).toMatch(/same source column/);
  });
});

describe("api client for writes", () => {
  it("returns the commit body for a recorded-but-rejected batch and throws for real errors", async () => {
    const rejected = { ok: false, replay: false, batch: { ...batch, status: "REJECTED" } };
    mockApi({
      "POST /ingestion/commit": () => jsonResponse(rejected, 422),
      "POST /ingestion/sources": () => jsonResponse({ error: { code: "write_api_disabled", message: "Writes are off.", request_id: "r1" } }, 403),
    });
    const res = await api.commit(new Blob(["a"]), { entity_type: "SUPPLIER", format: "CSV", source_id: 1 });
    expect(res.ok).toBe(false);
    expect(res.batch.status).toBe("REJECTED");
    await expect(api.createSource({ namespace: "x1", name: "x" })).rejects.toMatchObject({ status: 403, code: "write_api_disabled", requestId: "r1" });
  });
  it("sends raw file bodies with the right content type and never JSON-encodes them", async () => {
    const spy = mockApi({ "POST /ingestion/preview": () => jsonResponse({}) });
    const file = new Blob(["a,b\n1,2\n"]);
    await api.preview(file, { entity_type: "SUPPLIER", format: "CSV", source_id: 3, policy: "ATOMIC", delimiter: ";" });
    const init = spy.mock.calls[0]![1]!;
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("text/csv");
    expect(init.body).toBe(file);
    const url = new URL(String(spy.mock.calls[0]![0]));
    expect(url.searchParams.get("policy")).toBe("ATOMIC");
    expect(url.searchParams.get("delimiter")).toBe(";");
  });
  it("normalises connection failures", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed"));
    await expect(api.createProfile({ name: "n", entity_type: "SUPPLIER", definition: {} })).rejects.toBeInstanceOf(ApiError);
  });
});

describe("data operations dashboard", () => {
  it("shows counters, batches and sources from the API", async () => {
    mockApi({
      "GET /ingestion/summary": () => jsonResponse(summary),
      "GET /ingestion/batches": () => jsonResponse(page([batch])),
      "GET /ingestion/sources": () => jsonResponse(page([source])),
    });
    renderApp("/data");
    expect(await screen.findByText("Open findings")).toBeInTheDocument();
    expect(screen.getByText("3 error · 1 warning")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "BATCH-ABC" })).toHaveAttribute("href", "/data/batches/7");
    expect(screen.getByText("Completed with errors")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import data" })).toHaveAttribute("href", "/data/import");
  });
  it("has an empty state that points to the import wizard", async () => {
    mockApi({
      "GET /ingestion/summary": () => jsonResponse({ ...summary, batches: 0 }),
      "GET /ingestion/batches": () => jsonResponse(page([])),
      "GET /ingestion/sources": () => jsonResponse(page([])),
    });
    renderApp("/data");
    expect(await screen.findByText("No ingestion batches yet")).toBeInTheDocument();
  });
  it("shows an error state with retry when the backend is down", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    renderApp("/data");
    expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent(/cannot connect/i);
  });
});

describe("data quality explorer", () => {
  const routes = (extra: Record<string, Handler> = {}) => ({
    "GET /ingestion/rules": () => jsonResponse([{ rule_id: "DQ001", name: "missing_required_field", severity: "ERROR", description: "d" }]),
    "GET /ingestion/findings": () => jsonResponse(page([finding])),
    ...extra,
  });
  it("renders untrusted messages as text, not HTML", async () => {
    mockApi(routes());
    renderApp("/data/quality");
    const btn = await screen.findByRole("button", { name: /Required field 'name' is empty/ });
    expect(btn.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(document.querySelector("img")).toBeNull();
  });
  it("sends filters to the backend and reviews a finding", async () => {
    const spy = mockApi(
      routes({
        "PUT /ingestion/findings/11/resolution": ({ init }) => {
          expect(JSON.parse(String(init?.body))).toEqual({ resolution_status: "ACKNOWLEDGED", note: "seen" });
          return jsonResponse({ ...finding, resolution_status: "ACKNOWLEDGED", resolution_note: "seen" });
        },
      }),
    );
    renderApp("/data/quality");
    await userEvent.selectOptions(await screen.findByLabelText("Severity"), "ERROR");
    await waitFor(() => expect(spy.mock.calls.some((c) => new URL(String(c[0])).searchParams.get("severity") === "ERROR")).toBe(true));
    await userEvent.click(await screen.findByRole("button", { name: /Required field/ }));
    const detail = screen.getByRole("complementary", { name: "Finding details" });
    expect(within(detail).getByText(/Suggestion: Fill it in/)).toBeInTheDocument();
    await userEvent.type(within(detail).getByLabelText("Review note"), "seen");
    await userEvent.click(within(detail).getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(spy.mock.calls.some((c) => (c[1] as RequestInit | undefined)?.method === "PUT")).toBe(true));
  });
  it("surfaces a failed review without losing the finding", async () => {
    mockApi(routes({ "PUT /ingestion/findings/11/resolution": () => jsonResponse({ error: { code: "write_api_disabled", message: "Writes are off." } }, 403) }));
    renderApp("/data/quality");
    await userEvent.click(await screen.findByRole("button", { name: /Required field/ }));
    await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
    expect(await screen.findByText("Writes are off.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Required field/ })).toBeInTheDocument();
  });
  it("has an empty state", async () => {
    mockApi(routes({ "GET /ingestion/findings": () => jsonResponse(page([])) }));
    renderApp("/data/quality");
    expect(await screen.findByText("No data-quality findings")).toBeInTheDocument();
  });
  it("downloads the CSV export with the active filters and the bearer token, never a token in the URL", async () => {
    tokenStore.set("atl_s_synthetic", true);
    const spy = mockApi(routes({ "GET /ingestion/findings/export.csv": () => new Response("rule_id,DQ001", { status: 200 }) }));
    renderApp("/data/quality?severity=ERROR");
    await userEvent.click(await screen.findByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(spy.mock.calls.some((c) => String(c[0]).includes("/ingestion/findings/export.csv"))).toBe(true));
    const call = spy.mock.calls.find((c) => String(c[0]).includes("/ingestion/findings/export.csv"))!;
    const url = new URL(String(call[0]));
    expect(url.searchParams.get("severity")).toBe("ERROR");
    expect(url.search).not.toContain("atl_s_synthetic");
    expect((call[1]?.headers as Record<string, string>).Authorization).toBe("Bearer atl_s_synthetic");
    tokenStore.clear();
  });
  it("sends the bearer token on the original API client and ends the session on 401", async () => {
    tokenStore.set("atl_s_synthetic", true);
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ error: { code: "unauthenticated", message: "The session is not valid." } }, 401));
    await expect(api.suppliers({})).rejects.toBeInstanceOf(ApiError);
    expect((spy.mock.calls[0]![1]!.headers as Record<string, string>).Authorization).toBe("Bearer atl_s_synthetic");
    expect(tokenStore.get()).toBe("");
    expect(tokenStore.expired()).toBe(true);
    tokenStore.clear();
  });
});

describe("batch detail and lineage", () => {
  it("shows provenance, counts and row outcomes with original line numbers", async () => {
    mockApi({
      "GET /ingestion/batches/7": () => jsonResponse(batch),
      "GET /ingestion/batches/7/rows": () =>
        jsonResponse(page([{ id: 1, batch_id: 7, row_index: 4, source_line: 5, external_id: "S-4", outcome: "REJECTED", entity_id: null, record_fingerprint: null, message: "DQ001: Required" }])),
    });
    renderApp("/data/batches/7");
    expect(await screen.findByRole("heading", { name: "BATCH-ABC" })).toBeInTheDocument();
    expect(screen.getByText("canonical:1")).toBeInTheDocument();
    expect(await screen.findByText("DQ001: Required")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "2 findings" })).toHaveAttribute("href", "/data/quality?batch=7");
  });
  it("shows not-found for an unknown batch without retry", async () => {
    mockApi({});
    renderApp("/data/batches/999");
    expect(await screen.findByRole("alert")).toHaveTextContent("Not found");
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });
});

describe("mapping profiles", () => {
  const profile: MappingProfile = {
    id: 3, name: "erp supplier", version: 2, entity_type: "SUPPLIER", source_format: "ANY", source_id: null, status: "DRAFT",
    definition: { columns: { external_id: { source: "Id" }, name: { source: "Name" } }, unknown_field_policy: "warn", date_format: "%Y-%m-%d", null_tokens: [], unit_rules: {} },
    definition_hash: "h", description: null, created_at: "2026-10-19T10:00:00Z", activated_at: null,
  };
  it("lists versions, validates a new version via the API and shows server problems", async () => {
    mockApi({
      "GET /ingestion/mapping-profiles": () => jsonResponse(page([profile])),
      "GET /ingestion/formats": () => jsonResponse(formats),
      "POST /ingestion/mapping-profiles/validate": () => jsonResponse({ valid: false, problems: ["Required field 'category' has no mapping."] }),
    });
    renderApp("/data/mappings");
    expect(await screen.findByText("v2")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "New version" }));
    const editor = await screen.findByRole("region", { name: "Profile editor" });
    expect(within(editor).getByLabelText("Source column for external_id")).toHaveValue("Id");
    await userEvent.click(within(editor).getByRole("button", { name: "Validate" }));
    expect(await within(editor).findByText(/Required field 'category' has no mapping/)).toBeInTheDocument();
  });
  it("shows an empty state", async () => {
    mockApi({ "GET /ingestion/mapping-profiles": () => jsonResponse(page([])), "GET /ingestion/formats": () => jsonResponse(formats) });
    renderApp("/data/mappings");
    expect(await screen.findByText("No custom mapping profiles")).toBeInTheDocument();
  });
});

describe("import wizard", () => {
  const preview = (over: Partial<PreviewResponse> = {}): PreviewResponse => ({
    persisted: false, fingerprint: "f".repeat(64), plan_digest: "d".repeat(64), policy: "PARTIAL", entity_type: "SUPPLIER", source_format: "CSV",
    source_namespace: "erp-a", mapping_profile_ref: "canonical:1", normalization_version: "1", file_name: "s.csv", content_sha256: "a".repeat(64),
    content_bytes: 50, would_apply: true, already_applied_batch: null,
    counts: { total_rows: 3, to_create: 2, to_update: 0, unchanged: 0, rejected: 1, duplicates: 0 }, finding_counts: { ERROR: 1 },
    findings: [{ rule_id: "DQ001", severity: "ERROR", row_index: 3, source_line: 4, external_id: "S-3", field: "name", message: "Required field 'name' is empty.", suggestion: null, observed_value: null }],
    findings_truncated: false, rows: [], rows_truncated: false, columns: ["external_id", "name"], ...over,
  });
  const inspect = { format: "CSV", encoding: "utf-8", delimiter: ",", columns: ["external_id", "name", "country_or_region", "category"], row_count: 3, sample_rows: [{ external_id: "S-1", name: "<b>Acme</b>", country_or_region: "DE", category: "X" }], file_problems: [], suggested_columns: {}, unmapped_columns: [], canonical_fields: [] };
  const base = (extra: Record<string, Handler> = {}) => ({
    "GET /ingestion/sources": () => jsonResponse(page([source])),
    "GET /ingestion/formats": () => jsonResponse(formats),
    "POST /ingestion/inspect": () => jsonResponse(inspect),
    "POST /ingestion/preview": () => jsonResponse(preview()),
    ...extra,
  });

  async function advanceToPreview(user: ReturnType<typeof userEvent.setup>, fileText = "external_id,name\nS-1,A\n") {
    renderApp("/data/import");
    await user.selectOptions(await screen.findByLabelText("Existing source"), "1");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" })); // entity
    await user.upload(screen.getByLabelText("File"), new File([fileText], "s.csv", { type: "text/csv" }));
    await user.click(screen.getByRole("button", { name: "Inspect file" }));
    await screen.findByText(/3 data rows/);
    await user.click(screen.getByRole("button", { name: "Next" })); // detect -> mapping
    await user.click(screen.getByRole("button", { name: "Continue" })); // canonical mapping
    await user.click(screen.getByRole("button", { name: "Run preview" }));
    await screen.findByText(/Preview only — nothing has been stored/);
  }

  it("walks all ten steps, escapes file content, requires review of errors and sends the plan digest", async () => {
    const user = userEvent.setup();
    const commit = vi.fn<Handler>(() => jsonResponse({ ok: true, replay: false, batch }, 201));
    mockApi(base({ "POST /ingestion/commit": commit }));
    await advanceToPreview(user);
    expect(screen.getByRole("listitem", { current: "step" })).toHaveTextContent("7. Preview");
    await user.click(screen.getByRole("button", { name: "Next" })); // -> review
    expect(screen.getByText("Required field 'name' is empty.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" })); // -> commit
    const commitBtn = screen.getByRole("button", { name: "Commit" });
    expect(commitBtn).toBeDisabled(); // errors must be acknowledged first
    await user.click(screen.getByLabelText(/I have reviewed the 1 errors/));
    await user.click(commitBtn);
    expect(await screen.findByText("Import applied.")).toBeInTheDocument();
    const url = commit.mock.calls[0]![0].url;
    expect(url.searchParams.get("expected_plan_digest")).toBe("d".repeat(64));
    expect(url.searchParams.get("source_id")).toBe("1");
    expect(screen.getByRole("link", { name: /View batch BATCH-ABC/ })).toHaveAttribute("href", "/data/batches/7");
  });

  it("renders sample cells as text (no HTML injection)", async () => {
    const user = userEvent.setup();
    mockApi(base());
    renderApp("/data/import");
    await user.selectOptions(await screen.findByLabelText("Existing source"), "1");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.upload(screen.getByLabelText("File"), new File(["x"], "s.csv"));
    await user.click(screen.getByRole("button", { name: "Inspect file" }));
    expect(await screen.findByText("<b>Acme</b>")).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });

  it("blocks oversize files before any upload", async () => {
    const user = userEvent.setup();
    const spy = mockApi(base());
    renderApp("/data/import");
    await user.selectOptions(await screen.findByLabelText("Existing source"), "1");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.upload(screen.getByLabelText("File"), new File(["x".repeat(2000)], "big.csv"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/limit is 1,000 bytes/);
    expect(screen.getByRole("button", { name: "Inspect file" })).toBeDisabled();
    expect(spy.mock.calls.some((c) => String(c[0]).includes("/inspect"))).toBe(false);
  });

  it("handles a stale preview by asking for a new one and never reports success", async () => {
    const user = userEvent.setup();
    mockApi(base({ "POST /ingestion/commit": () => jsonResponse({ error: { code: "stale_preview", message: "changed" } }, 409) }));
    await advanceToPreview(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByLabelText(/I have reviewed/));
    await user.click(screen.getByRole("button", { name: "Commit" }));
    expect(await screen.findByText(/changed since this preview/)).toBeInTheDocument();
    expect(screen.queryByText("Import applied.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Re-run preview" })).toBeInTheDocument();
  });

  it("reports a rejected atomic batch as not applied", async () => {
    const user = userEvent.setup();
    const rejected = { ok: false, replay: false, batch: { ...batch, status: "REJECTED", created_count: 0, skipped_count: 2, rejected_count: 1, failure_reason: "Atomic policy: nothing was applied." } };
    mockApi(base({ "POST /ingestion/commit": () => jsonResponse(rejected, 422) }));
    await advanceToPreview(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByLabelText(/I have reviewed/));
    await user.click(screen.getByRole("button", { name: "Commit" }));
    expect(await screen.findByText("Nothing was applied.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Nothing was applied.");
    expect(screen.queryByText("Import applied.")).not.toBeInTheDocument();
  });

  it("disables commit when nothing would be applied", async () => {
    const user = userEvent.setup();
    mockApi(base({ "POST /ingestion/preview": () => jsonResponse(preview({ would_apply: false, policy: "ATOMIC" })) }));
    await advanceToPreview(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("button", { name: "Commit" })).toBeDisabled();
    expect(screen.getByText(/Commit is disabled because nothing would be applied/)).toBeInTheDocument();
  });

  it("creates a source inline and shows API validation errors", async () => {
    const user = userEvent.setup();
    mockApi(base({ "POST /ingestion/sources": () => jsonResponse({ error: { code: "conflict", message: "Namespace 'erp-a' is already registered." } }, 409) }));
    renderApp("/data/import");
    await screen.findByLabelText("Existing source");
    await user.type(screen.getByLabelText(/Namespace/), "erp-a");
    await user.type(screen.getByLabelText("Name"), "ERP");
    await user.click(screen.getByRole("button", { name: "Register source" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already registered");
  });
});

describe("products and lineage", () => {
  it("lists products and shows links on the detail page", async () => {
    const product = { id: 2, product_code: "DEMO-PRD-001", name: "Synthetic Microcontroller", category: "Electronics", unit_of_measure: "EA", status: "ACTIVE", provenance: "SYNTHETIC_DEMO", source_namespace: "atlas-demo", external_id: "DEMO-PRD-001", created_at: "2026-10-19T10:00:00Z", updated_at: "2026-10-19T10:00:00Z" };
    mockApi({
      "GET /products": () => jsonResponse(page([product])),
      "GET /products/2": () => jsonResponse({ ...product, links: [{ id: 1, link_code: "DEMO-PL-001", link_type: "SUPPLIES_PRODUCT", product_code: "DEMO-PRD-001", product_name: "x", supplier_code: "S", supplier_name: "Synthetic Supplier Alpha", facility_code: null, facility_name: null, quantity_value: null, quantity_unit: null, lead_time_days: 35, effective_from: null, effective_to: null, provenance: "SYNTHETIC_DEMO" }] }),
    });
    renderApp("/products");
    await userEvent.click(await screen.findByRole("link", { name: "Synthetic Microcontroller" }));
    expect(await screen.findByText("Synthetic Supplier Alpha")).toBeInTheDocument();
    expect(screen.getByText("unknown")).toBeInTheDocument(); // unknown quantity is shown as unknown, not 0
    expect(screen.getByText("35 d")).toBeInTheDocument();
  });
});
