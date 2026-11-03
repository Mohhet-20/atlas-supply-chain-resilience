// Mirrors the /routing API. Optional values are `null` when unknown: the UI never renders a missing figure as zero.
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
export type RunStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "PARTIAL" | "FAILED" | "CANCELLED";
export type ConstraintState = "SATISFIED" | "VIOLATED" | "UNVERIFIABLE";
export type Objective = "LOWEST_COST" | "FASTEST" | "MOST_RELIABLE" | "LOWEST_RISK" | "BALANCED" | "CUSTOM";
export const MODES = ["ROAD", "RAIL", "SEA", "AIR", "INLAND_WATERWAY", "PIPELINE", "MULTIMODAL"] as const;
export type Mode = (typeof MODES)[number];
export const CRITERIA = ["cost", "duration", "reliability", "risk"] as const;
export type Criterion = (typeof CRITERIA)[number];

export interface Health {
  algorithm_version: string;
  write_api_enabled: boolean;
  lanes: number;
  runs_by_status: Record<string, number>;
  modes: string[];
  limits: { max_lanes: number; max_active_runs: number; time_budget_seconds: number; max_expansions: number; max_hops: number; max_alternatives: number; min_reliability_sample: number; max_bulk_lanes: number };
  algorithms: string[];
  not_supported: string[];
  statements: Record<string, string>;
}
export interface ObjectivesDoc {
  criteria: { name: string; direction: string; unit: string }[];
  objectives: { name: string; weights: Record<string, number> | string; note?: string }[];
  normalisation: string;
  tie_break: string[];
}
export interface ConstraintsDoc {
  states: Record<string, string>;
  constraints: { name: string; judged_on: string; missing_data: string }[];
}

export interface NetworkNode {
  facility_id: number;
  code: string;
  name: string;
  country_or_region: string;
  coordinates: { latitude: number; longitude: number; status: string } | null;
}
export interface NetworkLane {
  lane_uid: string;
  origin_facility_id: number;
  destination_facility_id: number;
  mode: string;
  distance_km: number | null;
  duration_hours: number | null;
  cost: number | null;
  currency: string | null;
  capacity: number | null;
  capacity_unit: string | null;
  provenance: string;
  flags: string[];
}
export interface Issue {
  code: string;
  severity: string;
  lanes: string[];
  detail: string;
}
export interface NetworkView {
  network_hash: string;
  summary: { lanes: number; facilities: number; modes: Record<string, number>; excluded_lanes: number; issues: number };
  as_of: string | null;
  truncated: boolean;
  nodes: NetworkNode[];
  lanes: NetworkLane[];
  excluded: { lane_uid: string; reason: string }[];
  issues: Issue[];
  statement: string;
}
export interface LaneRecord {
  lane_uid: string;
  origin_facility_id: number;
  destination_facility_id: number;
  mode: string;
  distance_km: number | null;
  duration_hours: number | null;
  duration_quality: string;
  cost_amount: number | null;
  cost_redacted?: boolean;
  currency: string | null;
  cost_quality: string;
  capacity_amount: number | null;
  capacity_unit: string | null;
  reliability_prior: number | null;
  restrictions: string[];
  status: string;
  provenance: string;
  observed_at: string | null;
}

export interface Constraint {
  constraint: string;
  state: ConstraintState;
  limit: unknown;
  observed: unknown;
  detail: string;
}
export interface RouteMetrics {
  lane_uids: string[];
  facility_ids: number[];
  hops: number;
  transfers: number;
  modes: string[];
  distance_km: number | null;
  duration: { transit_hours: number | null; transfer_hours: number; total_hours: number | null; known_hours: number; status: string; quality: string; missing_lanes: string[] };
  cost: { total: number | null; currency: string | null; status: string; by_currency: Record<string, number>; missing_lanes: string[]; quality: string; scope: string };
  reliability: { value: number | null; upper_bound: number | null; status: string; missing_lanes: string[]; sources: string[]; assumption: string };
  risk: { value: number | null; coverage: number; facilities_without_score: number[]; basis: string };
  capacity: { bottleneck: number | null; unit: string | null; status: string };
  freshness: { oldest_observed_at: string | null; undated_lanes: string[] };
  provenance: string[];
  evidence_level: string;
  data_flags: string[];
  disruption_notes: string[];
}
export interface ScorePart {
  criterion: Criterion;
  value: number;
  direction: string;
  normalised: number;
  weight: number;
  contribution: number;
}
export interface RouteResult {
  rank: number;
  metrics: RouteMetrics;
  constraints: Constraint[];
  score: { composite: number; parts: ScorePart[] };
  pareto_optimal: boolean;
  overlap_with_best: number;
  trade_offs: Record<Criterion, number | null> | null;
  explanation: { summary: string; criteria: ScorePart[]; binding_constraints: string[]; caveats: string[] };
}
export interface Unverifiable {
  metrics: RouteMetrics;
  constraints: Constraint[];
  reasons: string[];
}
export interface Rejection {
  lane_uids: string[];
  modes: string[];
  total_hours: number | null;
  cost: number | null;
  currency: string | null;
  violations: { constraint: string; limit: unknown; observed: unknown; detail: string }[];
}
export interface Infeasibility {
  reasons: string[];
  network_connected: boolean;
  min_hops?: number | null;
  near_misses: Rejection[];
  relaxation: string;
}
export interface Rejections {
  count: number;
  by_constraint: Record<string, number>;
  examples: Rejection[];
  cut_before_completion: Record<string, number>;
  cut_note: string;
  infeasibility?: Infeasibility | null;
}
export interface SearchInfo {
  expansions: number;
  complete_candidates: number;
  pruned_branches: number;
  budget: number;
  truncated: boolean;
  yen_fallback_used: boolean;
  max_hops: number;
  optimality: string;
}
export interface RunSummary {
  status: string;
  objective: Objective;
  weights: Record<string, number>;
  ranking_currency: string | null;
  route_count: { feasible: number; unverifiable: number; rejected: number };
  unverifiable: Unverifiable[];
  pareto_front: string[][];
  references: Record<string, { lane_uids: string[]; value: number; rank: number }>;
  search: SearchInfo;
  baseline?: { status: string; route_count: { feasible: number }; search: SearchInfo };
}
export interface Comparison {
  status_change: string | null;
  baseline_status: string;
  scenario_status: string;
  best_route_changed: boolean | null;
  baseline_best: string[] | null;
  scenario_best: string[] | null;
  best_route_deltas: Record<Criterion, number | null> | null;
  baseline_routes_in_scenario: { lane_uids: string[]; state: "UNAVAILABLE" | "CHANGED" | "UNCHANGED"; causes?: { lane_uid: string; state: string; cause: string | null }[]; deltas?: Record<Criterion, number | null>; classification?: string }[];
}
export interface Disruptions {
  scenario: { scenario_id: number; scenario_uid: string; name: string; version: number; definition_hash: string } | null;
  applied: { source: string; kind: string; label?: string; lanes_closed?: string[]; lanes_changed?: string[]; facilities?: number[] }[];
  not_applied: { source: string; event_type: string; reason: string }[];
  overlay_hash: string;
  baseline_unchanged: boolean;
}
export interface RunListItem {
  run_uid: string;
  kind: "OPTIMIZE" | "FEASIBILITY";
  status: RunStatus;
  result_status: string | null;
  requested_by: string;
  origin_facility_id: number;
  destination_facility_id: number;
  scenario_id: number | null;
  objective: Objective | null;
  algorithm_version: string;
  network_hash: string | null;
  scenario_hash: string | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  error: string | null;
  route_count: number | null;
  best_route: string[] | null;
}
export interface RunDetail extends RunListItem {
  request: Record<string, unknown>;
  request_hash: string;
  network_summary: NetworkView["summary"] | null;
  routes: RouteResult[] | null;
  baseline_routes: RouteResult[] | null;
  comparison: Comparison | null;
  rejections: Rejections | null;
  disruptions: Disruptions | null;
  summary: RunSummary | null;
  warnings: string[] | null;
  provenance: { lane_provenance: Record<string, number>; as_of: string; reliability_sources?: string[]; network_issues?: Issue[] } | null;
  reproducibility: { request_hash: string; algorithm_version: string; network_hash: string; scenario_hash: string | null; overlay_hash: string | null; as_of: string | null; note: string } | null;
  statements: string[];
}
export interface ScenarioOption {
  id: number;
  name: string;
  scenario_uid: string;
}
