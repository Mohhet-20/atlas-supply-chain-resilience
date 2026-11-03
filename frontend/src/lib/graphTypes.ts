export type NodeType = "SUPPLIER" | "FACILITY" | "PRODUCT";
export type RelType = "SUPPLIES" | "TRANSFERS_TO" | "SUPPLIES_PRODUCT" | "PRODUCES" | "STORES" | "OWNS";
export type EdgeStatus = "PROPOSED" | "CONFIRMED" | "REJECTED" | "INACTIVE";
export type Direction = "upstream" | "downstream" | "both";

export const NODE_TYPES: NodeType[] = ["SUPPLIER", "FACILITY", "PRODUCT"];
export const REL_TYPES: RelType[] = ["SUPPLIES", "TRANSFERS_TO", "SUPPLIES_PRODUCT", "PRODUCES", "STORES", "OWNS"];
export const EDGE_STATUSES: EdgeStatus[] = ["CONFIRMED", "PROPOSED", "INACTIVE", "REJECTED"];

export interface GNode {
  type: NodeType;
  id: number;
  code: string;
  name: string;
  lifecycle: string;
  detail: string | null;
  provenance: string;
  depth: number | null;
}

export interface GEdge {
  ref: string;
  type: RelType;
  source_type: NodeType;
  source_id: number;
  destination_type: NodeType;
  destination_id: number;
  status: EdgeStatus;
  status_reason: string | null;
  code: string;
  provenance: string;
  source_namespace: string | null;
  effective_from: string | null;
  effective_to: string | null;
  capacity_value: number | null;
  capacity_unit: string | null;
  lead_time_days: number | null;
  supply_share_pct: number | null;
  criticality: string | null;
  confidence: number | null;
  version: number | null;
  read_only: boolean;
  hop: number | null;
  created_by: string | null;
  updated_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface GraphResult {
  start: GNode | null;
  direction: Direction | null;
  nodes: GNode[];
  edges: GEdge[];
  nodes_returned: number;
  edges_returned: number;
  requested_depth: number | null;
  max_depth_reached: number | null;
  nodes_at_depth_limit: number | null;
  truncated: boolean;
  truncation_reasons: string[];
  limits: Record<string, number | null>;
  resolved_from: { type: string; id: number } | null;
}

export interface PathItem {
  length: number;
  nodes: GNode[];
  edges: GEdge[];
  relationship_types: RelType[];
  provenance: string[];
  statuses: EdgeStatus[];
}

export interface PathsResult {
  start: GNode;
  goal: GNode;
  mode: "directed" | "undirected";
  paths: PathItem[];
  found: boolean;
  shortest_length: number | null;
  truncated: boolean;
  truncation_reasons: string[];
  note: string;
}

export interface GraphConfig {
  relationship_types: { type: RelType; source: NodeType; destination: NodeType; editable: boolean; cycles: string; meaning: string }[];
  not_modelled: Record<string, string>;
  statuses: EdgeStatus[];
  default_statuses: EdgeStatus[];
  direction: string;
  limits: {
    max_depth: number;
    default_depth: number;
    max_nodes: number;
    default_nodes: number;
    max_edges: number;
    default_edges: number;
    max_paths: number;
    max_path_length: number;
    default_path_length: number;
  };
}

export interface HistoryEvent {
  id: number;
  occurred_at: string;
  action: string;
  from_status: string | null;
  to_status: string | null;
  actor: string;
  reason: string | null;
  changes: Record<string, unknown>;
}

export interface RelationshipDetail {
  relationship: GEdge;
  source: GNode | null;
  destination: GNode | null;
  history: HistoryEvent[];
  allowed_transitions: EdgeStatus[];
}

export interface Filters {
  nodeTypes: NodeType[];
  relTypes: RelType[];
  statuses: EdgeStatus[];
  includeOutOfPeriod: boolean;
}

export interface GraphQuery {
  type: NodeType;
  id: number;
  direction: Direction;
  depth: number;
  maxNodes: number;
  maxEdges: number;
  filters: Filters;
}

export const nodeKey = (type: string, id: number) => `${type}:${id}`;
export const sourceKey = (e: GEdge) => nodeKey(e.source_type, e.source_id);
export const destKey = (e: GEdge) => nodeKey(e.destination_type, e.destination_id);
