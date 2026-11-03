/** Types for the Disruption Scenario API. Everything here describes hypothetical analysis, never observed events. */
export type ScenarioStatus = "DRAFT" | "READY" | "ARCHIVED";
export type ScenarioType = "SUPPLIER" | "FACILITY" | "RELATIONSHIP" | "REGIONAL" | "MULTI_ENTITY" | "CUSTOM";
export type EventType =
  | "SUPPLIER_UNAVAILABLE"
  | "SUPPLIER_CAPACITY_REDUCTION"
  | "FACILITY_SHUTDOWN"
  | "FACILITY_CAPACITY_REDUCTION"
  | "RELATIONSHIP_UNAVAILABLE"
  | "LEAD_TIME_INCREASE"
  | "REGION_DISRUPTION"
  | "CUSTOM";
export type SimStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "PARTIAL" | "FAILED" | "CANCELLED";
export type EntityKind = "SUPPLIER" | "FACILITY" | "PRODUCT";

export interface Assumptions {
  downstream_depth: number;
  upstream_depth: number;
  include_upstream: boolean;
  include_ownership: boolean;
  include_alternatives: boolean;
  include_proposed_alternatives: boolean;
  as_of_date: string | null;
  assume_no_buffer: boolean;
  notes: string | null;
}

export interface Message {
  code: string;
  severity: "ERROR" | "WARNING" | "INFO";
  text: string;
  field: string | null;
}

export interface TargetRef {
  type: EntityKind;
  id: number;
  name?: string;
}

export interface ScenarioEvent {
  id: number;
  event_uid: string;
  position: number;
  event_type: EventType;
  label: string | null;
  target_type: EntityKind | null;
  target_id: number | null;
  relationship_ref: string | null;
  targets: TargetRef[] | null;
  region_label: string | null;
  effect: "UNAVAILABLE" | "REDUCTION" | null;
  magnitude_pct: number | null;
  added_lead_time_days: number | null;
  start_offset_days: number | null;
  duration_days: number | null;
  recovery_days: number | null;
  assumption_notes: string | null;
  input_provenance: string | null;
  validation_status: "UNVALIDATED" | "VALID" | "WARNING" | "INVALID";
  validation_messages: Message[];
}

export type EventBody = Partial<Omit<ScenarioEvent, "id" | "event_uid" | "position" | "validation_status" | "validation_messages" | "event_type">> & {
  event_type: EventType;
};

export interface Scenario {
  id: number;
  scenario_uid: string;
  name: string;
  description: string | null;
  scenario_type: ScenarioType;
  template_key: string | null;
  status: ScenarioStatus;
  version: number;
  assumptions: Assumptions;
  config_version: string;
  notes: string | null;
  owner: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  validated_version: number | null;
  validation_current: boolean;
  run_count: number | null;
  events?: ScenarioEvent[];
  event_count?: number;
  disclaimer: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Template {
  key: string;
  name: string;
  scenario_type: ScenarioType;
  description: string;
  events: { event_type: EventType; needs: string; [k: string]: unknown }[];
  notes: string;
  requires_target_selection: boolean;
  disclaimer: string;
}

export interface Limits {
  engine_version: string;
  config_version: string;
  max_events: number;
  max_targets_per_event: number;
  max_depth: number;
  max_nodes: number;
  max_edges: number;
  time_budget_seconds: number;
  max_active_runs: number;
  client_overridable: false;
  default_assumptions: Assumptions;
  kind: string;
  unsupported: Record<string, string>;
}

export interface ValidationReport {
  valid: boolean;
  can_run: boolean;
  error_count: number;
  warning_count: number;
  scenario_messages: Message[];
  events: { event_uid: string; status: ScenarioEvent["validation_status"]; messages: Message[] }[];
  checks: { number: number; name: string; passed: boolean }[];
  scenario_version: number;
  scenario_status: ScenarioStatus;
}

export interface RunSummary {
  entities_affected: number;
  direct: number;
  indirect: number;
  downstream: number;
  upstream: number;
  by_type: Record<string, number>;
  by_exposure_level: Record<string, number>;
  by_quantification: Record<string, number>;
  affected_relationships: number;
  affected_products: number;
  affected_facilities: number;
  affected_suppliers: number;
  capacity_at_risk_by_unit: Record<string, number>;
  capacity_at_risk_note: string;
  relationships_with_unknown_capacity: number;
  facility_capacity_lost_by_unit: Record<string, number>;
  alternatives_total: number;
  entities_with_alternatives: number;
  by_event: { event_uid: string; event_type: EventType; label: string | null; entities_reached: number; direct: number; window: { duration_days: number | null; total_days: number | null } }[];
  interpretation: string;
}

export interface Run {
  id: number;
  run_uid: string;
  scenario_id: number;
  scenario_name: string | null;
  scenario_version: number;
  status: SimStatus;
  requested_by: string;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  engine_version: string | null;
  config_hash: string | null;
  definition_hash: string;
  graph_hash: string | null;
  graph_snapshot: Record<string, unknown> | null;
  entities_examined: number;
  entities_affected: number;
  direct_count: number;
  indirect_count: number;
  truncated: boolean;
  truncation_reasons: string[];
  cancel_requested: boolean;
  complete: boolean;
  warnings: { code: string; text: string }[];
  missing_data: { code: string; text: string; count?: number }[];
  failures: { event_uid?: string; error: string }[];
  error: string | null;
  assumptions: Assumptions | null;
  summary: RunSummary | null;
  scenario_snapshot?: { scenario: { name: string }; events: { event_uid: string; event_type: EventType; label: string | null }[] };
  disclaimer: string;
}

export interface PathStep {
  ref: string;
  type: string;
  from: { type: EntityKind; id: number; name: string | null };
  to: { type: EntityKind; id: number; name: string | null };
  supply_share_pct: number | null;
}

export interface Impact {
  id: number;
  entity_type: EntityKind;
  entity_id: number;
  entity_code: string;
  entity_name: string;
  impact_kind: "DIRECT" | "DOWNSTREAM" | "UPSTREAM";
  depth: number;
  event_uids: string[];
  quantification: "QUANTIFIED" | "LOWER_BOUND" | "UNQUANTIFIED";
  exposure_level: string;
  loss_pct: number | null;
  loss_pct_lower_bound: number | null;
  capacity_lost_value: number | null;
  capacity_unit: string | null;
  added_lead_time_days: number | null;
  path_count: number;
  primary_path: PathStep[];
  parent_edges: string[];
  rules: string[];
  uncertainty: string[];
  explanation: string;
  potential_exposure: true;
}

export interface ImpactPage extends Page<Impact> {
  run_status: SimStatus;
  complete: boolean;
}

export interface EdgeImpact {
  edge_ref: string;
  edge_type: string;
  source_type: EntityKind;
  source_id: number;
  destination_type: EntityKind;
  destination_id: number;
  effect: "UNAVAILABLE" | "REDUCED" | "DELAYED" | "EXPOSED";
  supply_share_pct: number | null;
  capacity_value: number | null;
  capacity_unit: string | null;
  lead_time_days: number | null;
  added_lead_time_days: number | null;
  event_uids: string[];
}

export interface Alternative {
  for_type: EntityKind;
  for_id: number;
  source_type: EntityKind;
  source_id: number;
  source_code: string;
  source_name: string;
  edge_ref: string;
  edge_type: string;
  edge_status: string;
  supply_share_pct: number | null;
  capacity_value: number | null;
  capacity_unit: string | null;
  lead_time_days: number | null;
  confidence: number | null;
  missing: string[];
  caveat: string;
}

export interface ImpactDetail {
  impact: Impact;
  path: PathStep[];
  parent_relationships: EdgeImpact[];
  path_relationships: EdgeImpact[];
  events: { event_uid: string; event_type: EventType; label: string | null; duration_days: number | null }[];
  rules_applied: string[];
  other_paths: number;
  note: string;
}

export interface Comparison {
  run_ids: number[];
  comparable: boolean;
  not_comparable_reasons: { code: string; text: string }[];
  warnings: { code: string; text: string }[];
  metric_comparability: { counts_and_sets: boolean; capacity: boolean; events: boolean };
  assumption_differences: string[];
  runs: {
    run_id: number;
    run_uid: string;
    scenario_id: number;
    scenario_version: number;
    status: SimStatus;
    entities_affected: number;
    direct: number;
    indirect: number;
    affected_products: number | null;
    affected_facilities: number | null;
    affected_relationships: number | null;
    capacity_at_risk_by_unit: Record<string, number>;
    relationships_with_unknown_capacity: number | null;
    alternatives_total: number | null;
    missing_data_codes: string[];
    warning_codes: string[];
    truncated: boolean;
  }[];
  entities: {
    in_all_runs: number;
    only_in_some_runs: number;
    total: number;
    truncated: boolean;
    rows: { entity_type: EntityKind; entity_id: number; entity_name: string; per_run: Record<string, { impact_kind: string; exposure_level: string; loss_pct: number | null }> }[];
  };
  same_scenario: boolean;
  note: string;
}

export interface ScenarioFilters {
  status?: ScenarioStatus | "";
  scenario_type?: ScenarioType | "";
  q?: string;
  limit: number;
  offset: number;
}
