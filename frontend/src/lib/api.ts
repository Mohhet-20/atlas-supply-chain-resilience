import { API_BASE_URL, apiUrl } from "./config";
import { authHeaders, downloadFile, noteUnauthorized } from "./token";
import type * as I from "./ingestionTypes";
import type {
  DashboardSummary,
  EntityStatus,
  Facility,
  FacilityDetail,
  FacilityType,
  NetworkSummary,
  Observation,
  Page,
  Provenance,
  Readiness,
  Supplier,
  SupplierDetail,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(message: string, status: number, code: string, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }

  get isConnectionError(): boolean {
    return this.code === "network_error";
  }
}

type Params = Record<string, string | number | undefined | null>;

function buildUrl(path: string, params?: Params): string {
  const url = apiUrl(path);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }
  return url.toString();
}

/** Single choke point for HTTP: centralised error normalisation, no silent fallbacks. */
export async function request<T>(path: string, params?: Params, signal?: AbortSignal): Promise<T> {
  const auth = authHeaders();
  let response: Response;
  try {
    response = await fetch(buildUrl(path, params), { signal, headers: { Accept: "application/json", ...auth } });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(`Cannot reach the ATLAS API at ${API_BASE_URL}. Is the backend running?`, 0, "network_error");
  }
  noteUnauthorized(response.status, Boolean(auth.Authorization));
  if (!response.ok) {
    let code = "http_error";
    let message = `Request failed with status ${response.status}.`;
    let requestId: string | null = response.headers.get("x-request-id");
    try {
      const body = (await response.json()) as { error?: { code?: string; message?: string; request_id?: string | null } };
      if (body.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        requestId = body.error.request_id ?? requestId;
      }
    } catch {
      /* non-JSON error body: keep generic message */
    }
    throw new ApiError(message, response.status, code, requestId);
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("The API returned an unreadable response.", response.status, "invalid_response");
  }
}

export interface SendOptions {
  params?: Params;
  body?: BodyInit | object;
  contentType?: string;
  signal?: AbortSignal;
}

/** POST/PUT with the same error normalisation as `request`. Non-Blob/string bodies are sent as JSON. */
export async function send<T>(method: "POST" | "PUT", path: string, opts: SendOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...authHeaders() };
  let body: BodyInit | undefined;
  if (opts.body instanceof Blob || typeof opts.body === "string") {
    body = opts.body;
    if (opts.contentType) headers["Content-Type"] = opts.contentType;
  } else if (opts.body !== undefined) {
    body = JSON.stringify(opts.body);
    headers["Content-Type"] = "application/json";
  }
  let response: Response;
  try {
    response = await fetch(buildUrl(path, opts.params), { method, headers, body, signal: opts.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(`Cannot reach the ATLAS API at ${API_BASE_URL}. Is the backend running?`, 0, "network_error");
  }
  noteUnauthorized(response.status, Boolean(headers.Authorization));
  let payload: unknown = undefined;
  try {
    payload = await response.json();
  } catch {
    /* handled below */
  }
  // Commit answers 422/409 with a CommitResponse body when a batch was recorded but not applied.
  const asCommit = payload as { batch?: unknown; error?: unknown } | undefined;
  if (!response.ok && !(asCommit && asCommit.batch !== undefined && asCommit.error === undefined)) {
    const err = (payload as { error?: { code?: string; message?: string; request_id?: string | null } } | undefined)?.error;
    throw new ApiError(
      err?.message ?? `Request failed with status ${response.status}.`,
      response.status,
      err?.code ?? "http_error",
      err?.request_id ?? response.headers.get("x-request-id"),
    );
  }
  if (payload === undefined) throw new ApiError("The API returned an unreadable response.", response.status, "invalid_response");
  return payload as T;
}

export type ListParams = {
  limit?: number;
  offset?: number;
  q?: string;
};

export const api = {
  readiness: (signal?: AbortSignal) => request<Readiness>("/readiness", undefined, signal),
  dashboard: (signal?: AbortSignal) => request<DashboardSummary>("/dashboard/summary", undefined, signal),
  network: (signal?: AbortSignal) => request<NetworkSummary>("/network/summary", undefined, signal),
  suppliers: (p: ListParams & { status?: EntityStatus; provenance?: Provenance }, signal?: AbortSignal) =>
    request<Page<Supplier>>("/suppliers", p, signal),
  supplier: (id: number, signal?: AbortSignal) => request<SupplierDetail>(`/suppliers/${id}`, undefined, signal),
  facilities: (
    p: ListParams & { status?: EntityStatus; facility_type?: FacilityType; provenance?: Provenance },
    signal?: AbortSignal,
  ) => request<Page<Facility>>("/facilities", p, signal),
  facility: (id: number, signal?: AbortSignal) => request<FacilityDetail>(`/facilities/${id}`, undefined, signal),
  observations: (p: ListParams, signal?: AbortSignal) => request<Page<Observation>>("/observations", p, signal),

  // ---- products
  products: (p: ListParams & { status?: EntityStatus; category?: string }, signal?: AbortSignal) =>
    request<Page<I.Product>>("/products", p, signal),
  product: (id: number, signal?: AbortSignal) => request<I.ProductDetail>(`/products/${id}`, undefined, signal),
  productLinks: (p: ListParams & { supplier_id?: number; facility_id?: number; product_id?: number }, signal?: AbortSignal) =>
    request<Page<I.ProductLink>>("/product-links", p, signal),

  // ---- ingestion
  ingestionSummary: (signal?: AbortSignal) => request<I.IngestionSummary>("/ingestion/summary", undefined, signal),
  formats: (signal?: AbortSignal) => request<I.FormatsResponse>("/ingestion/formats", undefined, signal),
  rules: (signal?: AbortSignal) => request<I.Rule[]>("/ingestion/rules", undefined, signal),
  sources: (signal?: AbortSignal) => request<Page<I.DataSource>>("/ingestion/sources", { limit: 100 }, signal),
  createSource: (body: { namespace: string; name: string; source_type?: string; description?: string }) =>
    send<I.DataSource>("POST", "/ingestion/sources", { body }),
  profiles: (p: ListParams & { entity_type?: I.EntityKind; status?: I.ProfileStatus }, signal?: AbortSignal) =>
    request<Page<I.MappingProfile>>("/ingestion/mapping-profiles", p, signal),
  validateProfile: (body: { entity_type: I.EntityKind; definition: object }) =>
    send<{ valid: boolean; problems: string[] }>("POST", "/ingestion/mapping-profiles/validate", { body }),
  createProfile: (body: {
    name: string;
    entity_type: I.EntityKind;
    source_format?: I.SourceFormat;
    source_id?: number | null;
    definition: object;
    description?: string;
  }) => send<I.MappingProfile>("POST", "/ingestion/mapping-profiles", { body }),
  activateProfile: (id: number) => send<I.MappingProfile>("POST", `/ingestion/mapping-profiles/${id}/activate`),
  retireProfile: (id: number) => send<I.MappingProfile>("POST", `/ingestion/mapping-profiles/${id}/retire`),
  batches: (p: ListParams & { status?: I.BatchStatus; source_id?: number }, signal?: AbortSignal) =>
    request<Page<I.Batch>>("/ingestion/batches", p, signal),
  batch: (id: number, signal?: AbortSignal) => request<I.Batch>(`/ingestion/batches/${id}`, undefined, signal),
  batchRows: (id: number, p: ListParams & { outcome?: I.RowOutcome }, signal?: AbortSignal) =>
    request<Page<I.RowResult>>(`/ingestion/batches/${id}/rows`, p, signal),
  findings: (
    p: ListParams & { batch_id?: number; rule_id?: string; severity?: I.Severity; resolution_status?: I.ResolutionStatus },
    signal?: AbortSignal,
  ) => request<Page<I.Finding>>("/ingestion/findings", p, signal),
  resolveFinding: (id: number, body: { resolution_status: I.ResolutionStatus; note?: string }) =>
    send<I.Finding>("PUT", `/ingestion/findings/${id}/resolution`, { body }),
  /** Downloaded with the bearer token; a plain link could not authenticate. */
  exportFindings: (p: { batch_id?: number; severity?: I.Severity; resolution_status?: I.ResolutionStatus }) =>
    downloadFile(buildUrl("/ingestion/findings/export.csv", p), "atlas-findings.csv"),
  lineage: (p: ListParams & { entity_type?: I.EntityKind; entity_id?: number; batch_id?: number }, signal?: AbortSignal) =>
    request<Page<I.Lineage>>("/lineage", p, signal),
  inspect: (file: Blob, p: IngestParams, signal?: AbortSignal) =>
    send<I.InspectResult>("POST", "/ingestion/inspect", { params: p, body: file, contentType: contentTypeFor(p.format), signal }),
  preview: (file: Blob, p: IngestParams, signal?: AbortSignal) =>
    send<I.PreviewResponse>("POST", "/ingestion/preview", { params: p, body: file, contentType: contentTypeFor(p.format), signal }),
  commit: (file: Blob, p: IngestParams, signal?: AbortSignal) =>
    send<I.CommitResponse>("POST", "/ingestion/commit", { params: p, body: file, contentType: contentTypeFor(p.format), signal }),
};

export type IngestParams = {
  entity_type: I.EntityKind;
  format: "CSV" | "JSON";
  source_id?: number;
  policy?: I.IngestPolicy;
  mapping_profile_id?: number | null;
  filename?: string;
  encoding?: string;
  delimiter?: string;
  expected_plan_digest?: string;
};

function contentTypeFor(format: "CSV" | "JSON"): string {
  return format === "CSV" ? "text/csv" : "application/json";
}
