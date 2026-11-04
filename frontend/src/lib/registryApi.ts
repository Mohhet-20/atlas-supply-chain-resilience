import { API_BASE_URL, apiUrl } from "./config";
import { ApiError } from "./api";
import type * as R from "./registryTypes";

import { tokenStore } from "./token";

export { tokenStore };

export type Params = Record<string, string | number | boolean | undefined | null | (string | number)[]>;
interface Opts {
  /** API area, e.g. "/registry" (default) or "/graph". The same bearer token is used for both. */
  prefix?: string;
  params?: Params;
  body?: unknown;
  /** Raw request body (a file) with its Content-Type. Used for imports; mutually exclusive with `body`. */
  raw?: { data: BodyInit; contentType: string };
  signal?: AbortSignal;
  /** Extra request headers (e.g. Idempotency-Key). Authorization and Content-Type are set by `call`. */
  headers?: Record<string, string>;
  /** Login and password-reset calls: a 401 there is a wrong credential, not an expired session. */
  keepTokenOn401?: boolean;
}

function url(path: string, params?: Params, prefix = "/registry"): string {
  const u = apiUrl(`${prefix}${path}`);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => u.searchParams.append(k, String(x)));
    else u.searchParams.set(k, String(v));
  }
  return u.toString();
}

export async function call<T>(method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE", path: string, opts: Opts = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...opts.headers };
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: string | undefined;
  let rawBody: BodyInit | undefined;
  if (opts.raw) {
    rawBody = opts.raw.data;
    headers["Content-Type"] = opts.raw.contentType;
  } else if (opts.body !== undefined) {
    body = JSON.stringify(opts.body);
    headers["Content-Type"] = "application/json";
  }
  let response: Response;
  try {
    response = await fetch(url(path, opts.params, opts.prefix), { method, headers, body: rawBody ?? body, signal: opts.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(`Cannot reach the ATLAS API at ${API_BASE_URL}. Is the backend running?`, 0, "network_error");
  }
  if (response.status === 204) return undefined as T;
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    /* handled below */
  }
  if (response.status === 401 && token && !opts.keepTokenOn401) tokenStore.expire();
  if (!response.ok) {
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

export interface SearchQuery {
  q: string;
  type?: R.RegistryType[];
  status?: string;
  country?: string;
  category?: string;
  include_merged?: boolean;
  fuzzy?: boolean;
  sort?: "relevance" | "name" | "updated";
  limit?: number;
  offset?: number;
}

export interface CandidateQuery {
  entity_type?: R.RegistryType;
  status?: R.CandidateStatus;
  tier?: R.CandidateTier[];
  min_score?: number;
  entity_id?: number;
  limit?: number;
  offset?: number;
}

export const registryApi = {
  me: (signal?: AbortSignal) => call<R.Me>("GET", "/me", { signal }),
  overview: (signal?: AbortSignal) => call<R.Overview>("GET", "/overview", { signal }),
  search: (p: SearchQuery, signal?: AbortSignal) => call<R.SearchResponse>("GET", "/search", { params: { ...p }, signal }),

  entity: (t: R.Plural, id: number, signal?: AbortSignal) => call<R.EntityDetail>("GET", `/${t}/${id}`, { signal }),
  create: (t: R.Plural, body: Record<string, unknown>) => call<R.EntityDetail>("POST", `/${t}`, { body }),
  update: (t: R.Plural, id: number, body: Record<string, unknown>) => call<R.EntityDetail>("PATCH", `/${t}/${id}`, { body }),
  retire: (id: number, expected_version: number) => call<R.EntityDetail>("POST", `/products/${id}/retire`, { body: { expected_version } }),
  reactivate: (id: number, expected_version: number) =>
    call<R.EntityDetail>("POST", `/products/${id}/reactivate`, { body: { expected_version } }),
  addAlias: (t: R.Plural, id: number, alias: string, kind: string) =>
    call<{ id: number }>("POST", `/${t}/${id}/aliases`, { body: { alias, kind } }),
  removeAlias: (t: R.Plural, id: number, aliasId: number) => call<void>("DELETE", `/${t}/${id}/aliases/${aliasId}`),
  addIdentifier: (t: R.Plural, id: number, body: { source_id: number; scheme: string; value: string; authoritative: boolean }) =>
    call<{ id: number }>("POST", `/${t}/${id}/identifiers`, { body }),
  removeIdentifier: (t: R.Plural, id: number, identId: number) => call<void>("DELETE", `/${t}/${id}/identifiers/${identId}`),

  candidates: (p: CandidateQuery, signal?: AbortSignal) => call<R.CandidateList>("GET", "/candidates", { params: { ...p }, signal }),
  candidate: (id: number, signal?: AbortSignal) => call<R.CandidateComparison>("GET", `/candidates/${id}`, { signal }),
  generate: (entity_type: R.RegistryType) => call<R.RunInfo>("POST", "/candidates/generate", { body: { entity_type } }),
  decide: (id: number, body: { decision: R.CandidateStatus; reason?: string; acknowledge_identifier_conflict?: boolean }) =>
    call<R.Candidate>("POST", `/candidates/${id}/decision`, { body }),

  previewMerge: (body: R.MergeRequestBody, signal?: AbortSignal) => call<R.MergePreview>("POST", "/merges/preview", { body, signal }),
  executeMerge: (body: R.MergeRequestBody) => call<R.MergeRecord>("POST", "/merges", { body }),
  merges: (p: { entity_type?: R.RegistryType; entity_id?: number; limit?: number; offset?: number }, signal?: AbortSignal) =>
    call<{ items: R.MergeRecord[]; total: number; limit: number; offset: number }>("GET", "/merges", { params: p, signal }),
  merge: (id: number, signal?: AbortSignal) => call<R.MergeRecord>("GET", `/merges/${id}`, { signal }),
  compensate: (id: number, reason: string) => call<R.MergeRecord>("POST", `/merges/${id}/compensate`, { body: { reason } }),

  qualityRun: () => call<R.QualitySummary["run"]>("POST", "/quality/run", { body: {} }),
  qualitySummary: (signal?: AbortSignal) => call<R.QualitySummary>("GET", "/quality/summary", { signal }),
  qualityConfig: (signal?: AbortSignal) => call<R.QualityConfigInfo>("GET", "/quality/config", { signal }),
  qualityIssues: (
    p: { entity_type?: R.RegistryType; dimension?: string; kind?: string; severity?: string; limit?: number; offset?: number },
    signal?: AbortSignal,
  ) => call<R.QualityIssuePage>("GET", "/quality/issues", { params: p, signal }),

  audit: (p: { action?: string; target_type?: string; target_id?: string; limit?: number; offset?: number }, signal?: AbortSignal) =>
    call<{ items: R.AuditEvent[]; total: number; limit: number; offset: number }>("GET", "/audit", { params: p, signal }),
};

/** Roles ordered by privilege, used only to *hide* controls the server would refuse anyway. The server is authoritative. */
export const ROLE_RANK: Record<R.Role, number> = { viewer: 0, editor: 1, reviewer: 2, admin: 3 };
export const can = (role: R.Role | undefined, needed: R.Role, writes = true): boolean =>
  writes && role !== undefined && ROLE_RANK[role] >= ROLE_RANK[needed];
