/** Types for the Cascading Impact API. Everything here describes structural, hypothetical analysis: never observed events. */
import type { EntityKind, Message, SimStatus } from "./scenarioTypes";

export type Classification = "INITIATING" | "DIRECT" | "PROPAGATED";
export type ImpactState = "SCENARIO_ASSUMED" | "QUANTIFIED" | "ASSUMPTION_DEPENDENT" | "POTENTIAL_EXPOSURE" | "INDETERMINATE";
export type DimStatus = "QUANTIFIED" | "LOWER_BOUND" | "QUALITATIVE" | "UNAVAILABLE" | "NOT_APPLICABLE";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ImpactLimits {
  algorithm_version: string;
  max_depth: number;
  max_nodes: number;
  max_edges: number;
  time_budget_seconds: number;
  max_path_steps: number;
  max_findings: number;
  max_active_analyses: number;
  client_overridable: boolean;
  defaults: Record<string, unknown>;
  scope: string;
  reachability_note: string;
  classifications: Record<string, string>;
  impact_states: Record<string, string>;
  disclaimer: string;
}

export interface Summary {
  scope: string;
  reachability_note: string;
  initiating_events: number;
  unique_entities: number;
  initiating: number;
  direct: number;
  propagated: number;
  suppliers_exposed: number;
  facilities_exposed: number;
  products_exposed: number;
  quantified: number;
  unquantified: number;
  indeterminate: number;
  distinct_paths_found: number;
  distinct_paths_exact: boolean;
  multi_path_entities: number;
  multi_event_entities: number;
  overlapping_entities: number;
  independent_entities: number;
  bottleneck_candidates: number;
  alternatives_total: number;
  entities_with_alternatives: number;
  counting_rule: string;
  by_state: Record<string, number>;
}

export interface Coverage {
  findings_with_inputs_applicable: number;
  mean_input_coverage: number | null;
  full_coverage: number;
  missing_inputs_by_field: Record<string, number>;
  evidence_strength: Record<string, number>;
  note: string;
}

export interface UncertaintyItem {
  code: string;
  count: number;
  text: string;
}

export interface Analysis {
  id: number;
  analysis_uid: string;
  run_id: number;
  scenario_id: number;
  scenario_name: string | null;
  scenario_version: number;
  status: SimStatus;
  requested_by: string;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  algorithm_version: string;
  config: Record<string, unknown>;
  config_hash: string;
  graph_hash: string | null;
  cancel_requested: boolean;
  truncated: boolean;
  truncation_reasons: string[];
  error: string | null;
  complete: boolean;
  summary: Summary | null;
  coverage: Coverage | null;
  warnings: Message[];
  failures: Message[];
  disclaimer: string;
  reproducibility: { algorithm_version: string; config_hash: string; graph_hash: string | null; run_id: number; note: string };
}

export interface SummaryResponse {
  analysis_id: number;
  status: SimStatus;
  summary: Summary;
  coverage: Coverage;
  uncertainty: UncertaintyItem[];
  excluded: Record<string, unknown>;
  warnings: Message[];
  truncated: boolean;
  truncation_reasons: string[];
  disclaimer: string;
}

export interface Finding {
  id: number;
  entity_type: EntityKind;
  entity_id: number;
  entity_code: string;
  entity_name: string;
  classification: Classification;
  min_depth: number;
  quantification: string;
  exposure_level: string | null;
  impact_state: ImpactState;
  loss_pct: number | null;
  loss_pct_lower_bound: number | null;
  added_lead_time_days: number | null;
  path_count: number;
  paths_complete: boolean;
  event_uids: string[];
  interaction: string | null;
  confidence: number | null;
  evidence_strength: string;
  coverage: number | null;
  key_uncertainty: string | null;
  uncertainty_count: number;
  evidence_count: number;
  alternatives_count: number;
  dimension_status: Record<string, DimStatus>;
  potential_exposure: boolean;
}

export interface Dimension {
  status: DimStatus;
  value?: number | null;
  unit?: string | null;
  reason?: string | null;
  note?: string | null;
}

export interface FindingDetail extends Finding {
  paths: { steps?: unknown[]; text?: string; edge_refs?: string[] }[];
  per_event: { event_uid: string; loss_pct: number | null; depth?: number; path_count?: number }[];
  combined: Record<string, unknown> | null;
  dimensions: Record<string, Dimension>;
  uncertainty: { code: string; text: string }[];
  evidence: { ref: string; supply_share_pct: number | null; lead_time_days: number | null }[];
  assumptions: string[];
  rules: string[];
  explanation: string;
}

export interface Criticality {
  id: number;
  entity_type: EntityKind;
  entity_id: number;
  entity_code: string;
  entity_name: string;
  role: string;
  downstream_reachable: number;
  products_reachable: number;
  facilities_reachable: number;
  paths_through: number;
  would_disconnect: number;
  would_disconnect_sample: string[];
  sole_known_source_of: string[];
  alternatives_for_downstream: number;
  bottleneck_candidate: boolean;
  reasons: string[];
  caveat: string;
  label: string;
}

export interface Alternative {
  id: number;
  for_type: EntityKind;
  for_id: number;
  for_name: string;
  source_type: EntityKind;
  source_id: number;
  source_name: string;
  path: string[];
  path_length: number;
  path_state: "ACTIVE" | "PROPOSED" | "UNCERTAIN" | "STALE";
  feasibility: string;
  capacity_value: number | null;
  capacity_unit: string | null;
  supply_share_pct: number | null;
  lead_time_days: number | null;
  missing: string[];
  caveat: string;
  operationally_validated: boolean;
}

export interface EventItem {
  event_uid: string;
  event_type: string;
  label: string | null;
  targets: { type: EntityKind; id: number; name: string | null }[];
  relationship_refs: string[];
  magnitude_pct: number | null;
  duration_days: number | null;
  entities_reached: number;
  reached_direct: number;
  timing: string;
  explanation: string;
}

export type GraphNode = Finding;
export interface GraphEdge {
  edge_ref: string;
  edge_type: string;
  source_type: EntityKind;
  source_id: number;
  destination_type: EntityKind;
  destination_id: number;
  supply_share_pct: number | null;
}
export interface GraphView {
  nodes: GraphNode[];
  edges: GraphEdge[];
  total_nodes: number;
  shown_nodes: number;
  truncated: boolean;
  note: string;
}

export interface ComparisonRun {
  analysis_id: number;
  run_id: number;
  status: SimStatus;
  unique_entities: number | null;
  initiating: number | null;
  direct: number | null;
  propagated: number | null;
  suppliers_exposed: number | null;
  facilities_exposed: number | null;
  products_exposed: number | null;
  quantified: number | null;
  unquantified: number | null;
  bottleneck_candidates: number | null;
  mean_input_coverage: number | null;
  truncated: boolean;
}
interface Delta {
  total: number;
  items: { entity: string; analysis_id?: number }[];
}
export interface ComparisonPair {
  baseline_analysis_id: number;
  other_analysis_id: number;
  newly_exposed: Delta;
  no_longer_exposed: Delta;
  persistently_exposed: number;
  depth_changes: { total: number };
  quantified_changes: { total: number };
  quantification_status_changes: { total: number };
  counts: Record<string, number>;
  coverage_delta: number | null;
  interpretation: string;
}
export interface Comparison {
  analysis_ids: number[];
  baseline_analysis_id: number;
  comparable: boolean;
  not_comparable_reasons: { code: string; text: string }[];
  warnings: { code: string; text: string }[];
  runs: ComparisonRun[];
  comparisons: ComparisonPair[];
  note: string;
}

export interface FindingFilters {
  limit: number;
  offset: number;
  entity_type?: string;
  classification?: string;
  impact_state?: string;
  q?: string;
  sort?: string;
}
