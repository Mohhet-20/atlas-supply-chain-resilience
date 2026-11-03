// Mirrors backend/app/schemas. Keep in sync with the OpenAPI document at /openapi.json.

export type Provenance = "SYNTHETIC_DEMO" | "USER_PROVIDED" | "IMPORTED_EXTERNAL" | "MODEL_ESTIMATE" | "SIMULATED";
export type EntityStatus = "ACTIVE" | "INACTIVE" | "ON_HOLD";
export type FacilityType = "MANUFACTURING" | "WAREHOUSE" | "DISTRIBUTION" | "CROSS_DOCK";
export type EntityType = "SUPPLIER" | "FACILITY";
export type RelationshipType = "SUPPLIES" | "TRANSFERS_TO";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Supplier {
  id: number;
  supplier_code: string;
  name: string;
  legal_name?: string | null;
  source_namespace?: string | null;
  external_id?: string | null;
  country_or_region: string;
  category: string;
  status: EntityStatus;
  provenance: Provenance;
  created_at: string;
  updated_at: string;
}

export interface Facility {
  id: number;
  facility_code: string;
  name: string;
  facility_type: FacilityType;
  country_or_region: string;
  city?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  location_status?: "UNKNOWN" | "UNVERIFIED" | "VERIFIED";
  capacity_value?: number | null;
  capacity_unit?: string | null;
  owner?: { id: number; supplier_code: string; name: string } | null;
  source_namespace?: string | null;
  external_id?: string | null;
  status: EntityStatus;
  provenance: Provenance;
  created_at: string;
  updated_at: string;
}

export interface EntityRef {
  type: EntityType;
  id: number;
  code: string;
  name: string;
}

export interface Relationship {
  id: number;
  relationship_code: string;
  relationship_type: RelationshipType;
  source: EntityRef;
  target: EntityRef;
  capacity_value: number | null;
  capacity_unit: string | null;
  lead_time_days: number | null;
  provenance: Provenance;
  created_at: string;
}

export interface Observation {
  id: number;
  observation_code: string;
  subject: EntityRef;
  metric_name: string;
  value: number;
  unit: string;
  observed_at: string;
  source_label: string;
  provenance: Provenance;
  quality_notes: string | null;
}

export interface SupplierDetail extends Supplier {
  relationships: Relationship[];
  recent_observations: Observation[];
}

export interface FacilityDetail extends Facility {
  inbound_relationships: Relationship[];
  outbound_relationships: Relationship[];
  recent_observations: Observation[];
}

export interface MetricResult {
  key: string;
  label: string;
  state: "ok" | "insufficient_data";
  value: number | null;
  unit: string | null;
  calculation: string;
  inputs: Record<string, number | string | null>;
  assumptions: string[];
  coverage: number | null;
  provenance: Record<string, number>;
  message: string | null;
  details: Record<string, number | string | null>[];
}

export interface RiskSummary {
  metrics: MetricResult[];
  disclaimer: string;
}

export interface DashboardSummary {
  supplier_count: number;
  facility_count: number;
  relationship_count: number;
  observation_count: number;
  provenance_counts: Record<string, number>;
  contains_synthetic_demo: boolean;
  coverage_indicator: number | null;
  coverage_state: string;
  recent_observations: Observation[];
  risk: RiskSummary;
}

export interface NetworkNode {
  type: EntityType;
  id: number;
  code: string;
  name: string;
  status: EntityStatus;
  provenance: Provenance;
}

export interface NetworkSummary {
  supplier_count: number;
  facility_count: number;
  relationship_count: number;
  nodes: NetworkNode[];
  edges: Relationship[];
  edges_returned: number;
  truncated: boolean;
  provenance_counts: Record<string, number>;
}

export interface Readiness {
  status: string;
  checks: Record<string, string>;
}
