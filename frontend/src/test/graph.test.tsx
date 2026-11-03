import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { layoutGraph, mergeGraphs } from "../lib/graphLayout";
import type { GEdge, GNode, GraphConfig, GraphResult, PathsResult } from "../lib/graphTypes";
import { tokenStore } from "../lib/registryApi";
import { jsonResponse } from "./fixtures";

type Handler = (url: URL) => Response;
let calls: { method: string; path: string; search: URLSearchParams }[] = [];
function mockApi(routes: Record<string, Handler>) {
  calls = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(String(input));
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api\/v1/, "");
    calls.push({ method, path, search: url.searchParams });
    const h = routes[`${method} ${path}`];
    return Promise.resolve(h ? h(url) : jsonResponse({ error: { code: "not_found", message: `no route ${method} ${path}` } }, 404));
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

const node = (type: GNode["type"], id: number, name: string, extra: Partial<GNode> = {}): GNode => ({
  type, id, code: `${type[0]}${id}`, name, lifecycle: "ACTIVE", detail: null, provenance: "USER_PROVIDED", depth: null, ...extra,
});
const edge = (ref: string, type: GEdge["type"], s: GNode, d: GNode, extra: Partial<GEdge> = {}): GEdge => ({
  ref, type, source_type: s.type, source_id: s.id, destination_type: d.type, destination_id: d.id, status: "CONFIRMED", status_reason: null,
  code: ref, provenance: "USER_PROVIDED", source_namespace: null, effective_from: null, effective_to: null, capacity_value: null, capacity_unit: null,
  lead_time_days: null, supply_share_pct: null, criticality: null, confidence: null, version: 1, read_only: false, hop: 1,
  created_by: "editor-user", updated_by: null, created_at: null, updated_at: null, ...extra,
});

const A = node("SUPPLIER", 1, "Alpha Metals");
const B = node("FACILITY", 2, "Beta Plant");
const C = node("PRODUCT", 3, "Gamma Widget");
const D = node("SUPPLIER", 4, "Delta Parts");
const e1 = edge("SUPPLY:1", "SUPPLIES", A, B);
const e2 = edge("PRODUCT:2", "PRODUCES", B, C);
const e3 = edge("SUPPLY:3", "SUPPLIES", D, A, { status: "CONFIRMED" });

const result = (nodes: GNode[], edges: GEdge[], extra: Partial<GraphResult> = {}): GraphResult => ({
  start: A, direction: "both", nodes, edges, nodes_returned: nodes.length, edges_returned: edges.length, requested_depth: 2, max_depth_reached: 1,
  nodes_at_depth_limit: 0, truncated: false, truncation_reasons: [], limits: {}, resolved_from: null, ...extra,
});

const config: GraphConfig = {
  relationship_types: [],
  not_modelled: {},
  statuses: ["PROPOSED", "CONFIRMED", "REJECTED", "INACTIVE"],
  default_statuses: ["CONFIRMED"],
  direction: "edges point from source to destination",
  limits: { max_depth: 6, default_depth: 2, max_nodes: 500, default_nodes: 200, max_edges: 1000, default_edges: 400, max_paths: 10, max_path_length: 8, default_path_length: 6 },
};
const me = jsonResponse({ actor: "viewer-user", role: "viewer", authenticated: true, auth_required: true, writes_enabled: true });
const baseRoutes = (expand: Handler): Record<string, Handler> => ({
  "GET /me": () => me.clone(),
  "GET /graph/config": () => jsonResponse(config),
  "GET /graph/expand": expand,
});

describe("graph layout", () => {
  it("places flow left to right and is deterministic", () => {
    const l1 = layoutGraph([A, B, C], [e1, e2]);
    const l2 = layoutGraph([C, B, A], [e2, e1]);
    const x = (l: ReturnType<typeof layoutGraph>, k: string) => l.index.get(k)?.x ?? -1;
    expect(x(l1, "SUPPLIER:1")).toBeLessThan(x(l1, "FACILITY:2"));
    expect(x(l1, "FACILITY:2")).toBeLessThan(x(l1, "PRODUCT:3"));
    expect(x(l2, "PRODUCT:3")).toBe(x(l1, "PRODUCT:3"));
  });
  it("survives cycles without hanging and reports back edges", () => {
    const t1 = edge("SUPPLY:10", "TRANSFERS_TO", B, node("FACILITY", 9, "Other Plant"));
    const t2 = edge("SUPPLY:11", "TRANSFERS_TO", node("FACILITY", 9, "Other Plant"), B);
    const l = layoutGraph([B, node("FACILITY", 9, "Other Plant")], [t1, t2]);
    expect(l.placed).toHaveLength(2);
    expect(l.backEdges.size).toBe(1);
  });
  it("merges graphs without duplicates and refuses to exceed the node cap", () => {
    const m = mergeGraphs([result([A, B], [e1]), result([B, C], [e1, e2])], 10);
    expect(m?.nodes).toHaveLength(3);
    expect(m?.edges).toHaveLength(2);
    expect(mergeGraphs([result([A, B], [e1]), result([C, D], [])], 3)).toBeNull();
  });
});

describe("Network explorer page", () => {
  it("asks for a starting record before loading anything", async () => {
    mockApi(baseRoutes(() => jsonResponse(result([A], []))));
    renderApp("/network/explorer");
    expect(await screen.findByText("Choose a record to explore")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/graph/expand")).toBe(false);
  });

  it("loads the neighbourhood from the URL with default CONFIRMED filter and shows counts", async () => {
    mockApi(baseRoutes(() => jsonResponse(result([A, B, C], [e1, e2]))));
    renderApp("/network/explorer?type=SUPPLIER&id=1");
    const bar = await screen.findByTestId("summary-bar");
    expect(bar).toHaveTextContent("3 nodes");
    expect(bar).toHaveTextContent("2 relationships");
    expect(screen.getByTestId("complete")).toBeInTheDocument();
    const q = calls.find((c) => c.path === "/graph/expand")!.search;
    expect(q.get("start_type")).toBe("SUPPLIER");
    expect(q.get("start_id")).toBe("1");
    expect(q.getAll("statuses")).toEqual(["CONFIRMED"]);
    expect(q.get("ignore_dates")).toBeNull();
  });

  it("flags a truncated result with its reason", async () => {
    mockApi(baseRoutes(() => jsonResponse(result([A, B], [e1], { truncated: true, truncation_reasons: ["node_limit"] }))));
    renderApp("/network/explorer?type=SUPPLIER&id=1");
    expect(await screen.findByTestId("truncated")).toHaveTextContent(/node limit/i);
    expect(screen.queryByTestId("complete")).toBeNull();
  });

  it("shows an empty state, not a blank canvas, when there are no relationships", async () => {
    mockApi(baseRoutes(() => jsonResponse(result([A], []))));
    renderApp("/network/explorer?type=SUPPLIER&id=1");
    expect(await screen.findByText("No relationships to show")).toBeInTheDocument();
  });

  it("explains a 403 and does not show a graph", async () => {
    mockApi(baseRoutes(() => jsonResponse({ error: { code: "forbidden", message: "Role viewer required" } }, 403)));
    renderApp("/network/explorer?type=SUPPLIER&id=1");
    expect(await screen.findByTestId("permission-denied")).toHaveTextContent("Not permitted");
  });

  it("asks for a token on 401", async () => {
    mockApi(baseRoutes(() => jsonResponse({ error: { code: "unauthorized", message: "Token required" } }, 401)));
    renderApp("/network/explorer?type=SUPPLIER&id=1");
    expect(await screen.findByTestId("permission-denied")).toHaveTextContent("Sign-in required");
  });

  it("exposes every drawn relationship in an accessible table and opens details", async () => {
    mockApi({
      ...baseRoutes(() => jsonResponse(result([A, B, C], [e1, e2]))),
      "GET /graph/relationships/SUPPLY%3A1": () =>
        jsonResponse({
          relationship: e1, source: A, destination: B, allowed_transitions: [],
          history: [{ id: 1, occurred_at: "2026-10-19T10:00:00Z", action: "STATUS_CHANGED", from_status: "PROPOSED", to_status: "CONFIRMED", actor: "reviewer-user", reason: "Verified with contract", changes: {} }],
        }),
    });
    const user = userEvent.setup();
    renderApp("/network/explorer?type=SUPPLIER&id=1");
    await screen.findByTestId("summary-bar");
    await user.click(screen.getByText(/Relationships shown, as a table/));
    const table = screen.getByRole("table", { name: /every relationship/i });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    await user.click(screen.getByRole("button", { name: "Details of SUPPLY:1" }));
    const region = await screen.findByRole("region", { name: "Relationship details" });
    expect(within(region).getByRole("heading", { level: 2 })).toHaveTextContent("Alpha Metals → Beta Plant");
    expect(await within(region).findByText(/Verified with contract/)).toBeInTheDocument();
    expect(within(region).getByText("Capacity").nextElementSibling).toHaveTextContent(/unknown/i);
  });

  it("expands a selected node's neighbours into the same canvas", async () => {
    let n = 0;
    mockApi(baseRoutes(() => jsonResponse(++n === 1 ? result([A, B], [e1]) : result([A, D], [e3], { start: A }))));
    const user = userEvent.setup();
    renderApp("/network/explorer?type=SUPPLIER&id=1");
    await screen.findByTestId("summary-bar");
    await user.click(screen.getByRole("button", { name: /Supplier S1, Alpha Metals/i }));
    await user.click(await screen.findByRole("button", { name: "Expand neighbours" }));
    await waitFor(() => expect(screen.getByTestId("summary-bar")).toHaveTextContent("3 nodes"));
    expect(screen.getByTestId("summary-bar")).toHaveTextContent("2 relationships");
    const second = calls.filter((c) => c.path === "/graph/expand")[1]!;
    expect(second.search.get("depth")).toBe("1");
  });

  it("finds a path and reports it as text", async () => {
    const paths: PathsResult = {
      start: A, goal: C, mode: "directed", found: true, shortest_length: 2, truncated: false, truncation_reasons: [],
      note: "Stored relationships only.",
      paths: [{ length: 2, nodes: [A, B, C], edges: [e1, e2], relationship_types: ["SUPPLIES", "PRODUCES"], provenance: ["USER_PROVIDED"], statuses: ["CONFIRMED"] }],
    };
    mockApi({
      ...baseRoutes(() => jsonResponse(result([A, B], [e1]))),
      "GET /graph/paths": () => jsonResponse(paths),
      "GET /registry/search": (u) =>
        jsonResponse({
          items: [{ type: u.searchParams.get("q") === "gam" ? "PRODUCT" : "SUPPLIER", id: u.searchParams.get("q") === "gam" ? 3 : 1, code: "X", name: u.searchParams.get("q") === "gam" ? "Gamma Widget" : "Alpha Metals", status: "ACTIVE", country_or_region: null, category: null, merged_into: null, source_namespace: null, external_id: null, updated_at: "2026-10-19T10:00:00Z", version: 1, match_reasons: [], exact: false, approximate: false, best_reason: null }],
          total: 1, limit: 8, offset: 0, truncated: false, notes: [],
        }),
    });
    const user = userEvent.setup();
    renderApp("/network/explorer?type=SUPPLIER&id=1");
    await screen.findByTestId("summary-bar");
    const panel = screen.getByRole("region", { name: "Path search" });
    await user.type(within(panel).getByLabelText("From record"), "alp");
    await user.click(await within(panel).findByRole("button", { name: /Alpha Metals/ }));
    await user.type(within(panel).getByLabelText("To record"), "gam");
    await user.click(await within(panel).findByRole("button", { name: /Gamma Widget/ }));
    await user.click(within(panel).getByRole("button", { name: "Find paths" }));
    const res = await within(panel).findByTestId("path-result");
    expect(res).toHaveTextContent("1 path found; shortest has 2 steps");
    expect(res).toHaveTextContent("Alpha Metals → Beta Plant → Gamma Widget");
    await waitFor(() => expect(screen.getByTestId("summary-bar")).toHaveTextContent("3 nodes"));
  });

  it("keeps Apply disabled when a filter group is emptied", async () => {
    mockApi(baseRoutes(() => jsonResponse(result([A, B], [e1]))));
    const user = userEvent.setup();
    renderApp("/network/explorer?type=SUPPLIER&id=1");
    await screen.findByTestId("summary-bar");
    await user.click(screen.getByRole("checkbox", { name: "Confirmed" }));
    expect(screen.getByRole("button", { name: "Apply to current focus" })).toBeDisabled();
  });
});
