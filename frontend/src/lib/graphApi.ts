import { call, type Params } from "./registryApi";
import type { Direction, Filters, GraphConfig, GraphQuery, GraphResult, NodeType, PathsResult, RelationshipDetail } from "./graphTypes";

const get = <T>(path: string, params?: Params, signal?: AbortSignal) => call<T>("GET", path, { prefix: "/graph", params, signal });

/** Query parameters shared by every traversal endpoint. Only non-default values are sent. */
export function filterParams(f: Filters): Params {
  return {
    node_types: f.nodeTypes.length === 3 ? undefined : f.nodeTypes,
    types: f.relTypes.length === 6 ? undefined : f.relTypes,
    statuses: f.statuses,
    ignore_dates: f.includeOutOfPeriod ? true : undefined,
  };
}

export const graphApi = {
  config: (signal?: AbortSignal) => get<GraphConfig>("/config", undefined, signal),
  expand: (q: GraphQuery, signal?: AbortSignal) =>
    get<GraphResult>(
      "/expand",
      { start_type: q.type, start_id: q.id, direction: q.direction, depth: q.depth, max_nodes: q.maxNodes, max_edges: q.maxEdges, ...filterParams(q.filters) },
      signal,
    ),
  neighbors: (type: NodeType, id: number, direction: Direction, f: Filters, signal?: AbortSignal) =>
    get<GraphResult>(`/nodes/${type}/${id}/neighbors`, { direction, max_edges: 200, ...filterParams(f) }, signal),
  paths: (
    p: { fromType: NodeType; fromId: number; toType: NodeType; toId: number; mode: "directed" | "undirected"; maxLength: number },
    f: Filters,
    signal?: AbortSignal,
  ) =>
    get<PathsResult>(
      "/paths",
      { from_type: p.fromType, from_id: p.fromId, to_type: p.toType, to_id: p.toId, mode: p.mode, max_length: p.maxLength, max_paths: 5, ...filterParams(f) },
      signal,
    ),
  relationship: (ref: string, signal?: AbortSignal) => get<RelationshipDetail>(`/relationships/${encodeURIComponent(ref)}`, undefined, signal),
  summary: (signal?: AbortSignal) => get<Record<string, unknown>>("/summary", undefined, signal),
};
