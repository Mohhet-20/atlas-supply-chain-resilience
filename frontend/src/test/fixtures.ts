import type { DashboardSummary, Facility, NetworkSummary, Page, Relationship, Supplier } from "../lib/types";

export const supplier: Supplier = {
  id: 1,
  supplier_code: "DEMO-SUP-001",
  name: "Synthetic Supplier Alpha",
  country_or_region: "Southeast Asia",
  category: "Electronic Components",
  status: "ACTIVE",
  provenance: "SYNTHETIC_DEMO",
  created_at: "2026-10-18T10:00:00Z",
  updated_at: "2026-10-18T10:00:00Z",
};

export const facility: Facility = {
  id: 1,
  facility_code: "DEMO-FAC-001",
  name: "Synthetic Plant One",
  facility_type: "MANUFACTURING",
  country_or_region: "Western Europe",
  status: "ACTIVE",
  provenance: "SYNTHETIC_DEMO",
  created_at: "2026-10-18T10:00:00Z",
  updated_at: "2026-10-18T10:00:00Z",
};

export const relationship: Relationship = {
  id: 1,
  relationship_code: "DEMO-REL-001",
  relationship_type: "SUPPLIES",
  source: { type: "SUPPLIER", id: 1, code: "DEMO-SUP-001", name: "Synthetic Supplier Alpha" },
  target: { type: "FACILITY", id: 1, code: "DEMO-FAC-001", name: "Synthetic Plant One" },
  capacity_value: 5000,
  capacity_unit: "units/week",
  lead_time_days: 14,
  provenance: "SYNTHETIC_DEMO",
  created_at: "2026-10-18T10:00:00Z",
};

export function page<T>(items: T[], total = items.length): Page<T> {
  return { items, total, limit: 20, offset: 0 };
}

export const network: NetworkSummary = {
  supplier_count: 1,
  facility_count: 1,
  relationship_count: 1,
  nodes: [
    { type: "SUPPLIER", id: 1, code: "DEMO-SUP-001", name: "Synthetic Supplier Alpha", status: "ACTIVE", provenance: "SYNTHETIC_DEMO" },
    { type: "FACILITY", id: 1, code: "DEMO-FAC-001", name: "Synthetic Plant One", status: "ACTIVE", provenance: "SYNTHETIC_DEMO" },
  ],
  edges: [relationship],
  edges_returned: 1,
  truncated: false,
  provenance_counts: { SYNTHETIC_DEMO: 3, USER_PROVIDED: 0, IMPORTED_EXTERNAL: 0, MODEL_ESTIMATE: 0, SIMULATED: 0 },
};

export const dashboard: DashboardSummary = {
  supplier_count: 1,
  facility_count: 1,
  relationship_count: 1,
  observation_count: 0,
  provenance_counts: network.provenance_counts,
  contains_synthetic_demo: true,
  coverage_indicator: null,
  coverage_state: "insufficient_data",
  recent_observations: [],
  risk: {
    disclaimer: "Baseline structural indicators.",
    metrics: [
      {
        key: "data_coverage_indicator",
        label: "Overall data coverage indicator",
        state: "insufficient_data",
        value: null,
        unit: null,
        calculation: "mean of components",
        inputs: {},
        assumptions: [],
        coverage: null,
        provenance: {},
        message: "Cannot compute: insufficient data for observation_coverage.",
        details: [],
      },
    ],
  },
};

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
