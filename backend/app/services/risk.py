"""Deterministic baseline risk/coverage metrics.

Pure functions over plain dataclasses (no DB, no network, no randomness). Nothing here is a
probability or a forecast: each metric reports inputs, the formula, assumptions, coverage and
provenance, or returns ``insufficient_data`` with an explanation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.models.enums import RelationshipType
from app.schemas.risk import MetricResult, RiskSummary

DISCLAIMER = (
    "Baseline structural indicators computed deterministically from stored records. They are not "
    "disruption probabilities or forecasts. Values derived from SYNTHETIC_DEMO records are not real-world intelligence."
)


@dataclass(frozen=True)
class FacilityRec:
    id: int
    code: str
    provenance: str


@dataclass(frozen=True)
class SupplierRec:
    id: int
    code: str
    provenance: str


@dataclass(frozen=True)
class RelRec:
    type: RelationshipType
    supplier_id: int | None
    source_facility_id: int | None
    target_facility_id: int
    capacity_value: float | None
    capacity_unit: str | None
    lead_time_days: float | None
    provenance: str


@dataclass(frozen=True)
class ObsRec:
    supplier_id: int | None
    facility_id: int | None
    provenance: str


@dataclass
class NetworkData:
    suppliers: list[SupplierRec] = field(default_factory=list)
    facilities: list[FacilityRec] = field(default_factory=list)
    relationships: list[RelRec] = field(default_factory=list)
    observations: list[ObsRec] = field(default_factory=list)


def _insufficient(key: str, label: str, calc: str, message: str, inputs: dict | None = None) -> MetricResult:
    return MetricResult(
        key=key, label=label, state="insufficient_data", calculation=calc, message=message, inputs=inputs or {}
    )


def _supplies(data: NetworkData) -> list[RelRec]:
    return [r for r in data.relationships if r.type is RelationshipType.SUPPLIES and r.supplier_id is not None]


def single_source_facilities(data: NetworkData) -> MetricResult:
    key, label = "single_source_facilities", "Facilities with a single inbound supplier"
    calc = "count of facilities whose inbound SUPPLIES relationships come from exactly one distinct supplier"
    supplies = _supplies(data)
    if not supplies:
        return _insufficient(key, label, calc, "No supplier-to-facility (SUPPLIES) relationships recorded.")
    suppliers_by_fac: dict[int, set[int]] = defaultdict(set)
    for r in supplies:
        suppliers_by_fac[r.target_facility_id].add(r.supplier_id)  # type: ignore[arg-type]
    codes = {f.id: f.code for f in data.facilities}
    singles = sorted(codes.get(fid, str(fid)) for fid, s in suppliers_by_fac.items() if len(s) == 1)
    total = len(data.facilities)
    return MetricResult(
        key=key,
        label=label,
        state="ok",
        value=float(len(singles)),
        unit="facilities",
        calculation=calc,
        inputs={
            "facilities_with_inbound_suppliers": len(suppliers_by_fac),
            "facilities_total": total,
            "single_source_facility_codes": ", ".join(singles) or None,
        },
        assumptions=[
            "Only recorded relationships are counted; unrecorded suppliers are invisible to this metric.",
            "Supplier status (active/on hold) is not considered.",
        ],
        coverage=len(suppliers_by_fac) / total if total else None,
        provenance=dict(Counter(r.provenance for r in supplies)),
    )


def supplier_dependency_concentration(data: NetworkData) -> MetricResult:
    key, label = "supplier_dependency_concentration", "Inbound supplier concentration (HHI, max across facilities)"
    calc = (
        "per facility: HHI = sum(share_i^2) over inbound suppliers; share by summed capacity when every inbound "
        "relationship has a capacity in one common unit, otherwise by relationship count; value = max HHI"
    )
    supplies = _supplies(data)
    if not supplies:
        return _insufficient(key, label, calc, "No supplier-to-facility (SUPPLIES) relationships recorded.")
    by_fac: dict[int, list[RelRec]] = defaultdict(list)
    for r in supplies:
        by_fac[r.target_facility_id].append(r)
    codes = {f.id: f.code for f in data.facilities}
    details: list[dict] = []
    for fid, rels in sorted(by_fac.items(), key=lambda kv: codes.get(kv[0], "")):
        units = {r.capacity_unit for r in rels}
        use_capacity = all(r.capacity_value is not None for r in rels) and len(units) == 1 and None not in units
        weights: dict[int, float] = defaultdict(float)
        for r in rels:
            weights[r.supplier_id] += r.capacity_value if use_capacity else 1.0  # type: ignore[index,operator]
        total_w = sum(weights.values())
        if total_w <= 0:  # all-zero capacity: fall back to counts
            use_capacity = False
            weights = defaultdict(float)
            for r in rels:
                weights[r.supplier_id] += 1.0  # type: ignore[index]
            total_w = sum(weights.values())
        hhi = sum((w / total_w) ** 2 for w in weights.values())
        details.append(
            {
                "facility_code": codes.get(fid, str(fid)),
                "hhi": round(hhi, 6),
                "supplier_count": len(weights),
                "basis": "capacity" if use_capacity else "relationship_count",
            }
        )
    worst = max(details, key=lambda d: (d["hhi"], d["facility_code"]))
    total = len(data.facilities)
    return MetricResult(
        key=key,
        label=label,
        state="ok",
        value=worst["hhi"],
        unit="HHI (1/n to 1)",
        calculation=calc,
        inputs={
            "max_hhi_facility": worst["facility_code"],
            "facilities_evaluated": len(details),
            "facilities_total": total,
        },
        assumptions=[
            "HHI of 1.0 means a single supplier; lower values mean more diversified supply.",
            "Basis is chosen per facility and shown in details; no risk threshold is asserted.",
        ],
        coverage=len(details) / total if total else None,
        provenance=dict(Counter(r.provenance for r in supplies)),
        details=details,
    )


def relationship_attribute_completeness(data: NetworkData) -> MetricResult:
    key, label = "relationship_attribute_completeness", "Relationship capacity/lead-time completeness"
    calc = "mean of (relationships with capacity / all) and (relationships with lead time / all)"
    rels = data.relationships
    if not rels:
        return _insufficient(key, label, calc, "No relationships recorded.")
    n = len(rels)
    cap = sum(1 for r in rels if r.capacity_value is not None)
    lead = sum(1 for r in rels if r.lead_time_days is not None)
    return MetricResult(
        key=key,
        label=label,
        state="ok",
        value=((cap / n) + (lead / n)) / 2,
        unit="ratio",
        calculation=calc,
        inputs={
            "relationships": n,
            "with_capacity": cap,
            "with_lead_time": lead,
            "missing_capacity": n - cap,
            "missing_lead_time": n - lead,
        },
        assumptions=["Equal weight for capacity and lead time."],
        coverage=1.0,
        provenance=dict(Counter(r.provenance for r in rels)),
    )


def connectivity_coverage(data: NetworkData) -> MetricResult:
    key, label = "connectivity_coverage", "Entities participating in at least one relationship"
    calc = "(suppliers + facilities appearing in any relationship) / (all suppliers + facilities)"
    total = len(data.suppliers) + len(data.facilities)
    if total == 0:
        return _insufficient(key, label, calc, "No suppliers or facilities recorded.")
    sup_ids = {r.supplier_id for r in data.relationships if r.supplier_id is not None}
    fac_ids = {r.target_facility_id for r in data.relationships} | {
        r.source_facility_id for r in data.relationships if r.source_facility_id is not None
    }
    connected_s = sum(1 for s in data.suppliers if s.id in sup_ids)
    connected_f = sum(1 for f in data.facilities if f.id in fac_ids)
    return MetricResult(
        key=key,
        label=label,
        state="ok",
        value=(connected_s + connected_f) / total,
        unit="ratio",
        calculation=calc,
        inputs={
            "suppliers": len(data.suppliers),
            "facilities": len(data.facilities),
            "suppliers_without_relationships": len(data.suppliers) - connected_s,
            "facilities_without_relationships": len(data.facilities) - connected_f,
        },
        assumptions=["Unconnected entities may be genuinely isolated or simply missing data."],
        coverage=1.0,
        provenance=dict(Counter([s.provenance for s in data.suppliers] + [f.provenance for f in data.facilities])),
    )


def observation_coverage(data: NetworkData) -> MetricResult:
    key, label = "observation_coverage", "Entities with at least one observation"
    calc = "(suppliers + facilities with >=1 observation) / (all suppliers + facilities)"
    total = len(data.suppliers) + len(data.facilities)
    if total == 0:
        return _insufficient(key, label, calc, "No suppliers or facilities recorded.")
    sup_ids = {o.supplier_id for o in data.observations if o.supplier_id is not None}
    fac_ids = {o.facility_id for o in data.observations if o.facility_id is not None}
    have_s = sum(1 for s in data.suppliers if s.id in sup_ids)
    have_f = sum(1 for f in data.facilities if f.id in fac_ids)
    return MetricResult(
        key=key,
        label=label,
        state="ok",
        value=(have_s + have_f) / total,
        unit="ratio",
        calculation=calc,
        inputs={
            "observations": len(data.observations),
            "entities_with_observations": have_s + have_f,
            "entities_total": total,
        },
        assumptions=["Counts presence of any observation, not its recency or quality."],
        coverage=1.0,
        provenance=dict(Counter(o.provenance for o in data.observations)),
    )


def data_coverage_indicator(data: NetworkData, parts: list[MetricResult]) -> MetricResult:
    key, label = "data_coverage_indicator", "Overall data coverage indicator"
    calc = "equal-weight mean of relationship completeness, connectivity coverage and observation coverage"
    if any(p.state != "ok" or p.value is None for p in parts):
        missing = [p.key for p in parts if p.state != "ok"]
        return _insufficient(
            key,
            label,
            calc,
            "Cannot compute: insufficient data for " + ", ".join(missing) + ".",
            {"missing_components": ", ".join(missing)},
        )
    return MetricResult(
        key=key,
        label=label,
        state="ok",
        value=sum(p.value for p in parts if p.value is not None) / len(parts),
        unit="ratio",
        calculation=calc,
        inputs={p.key: round(p.value or 0.0, 6) for p in parts},
        assumptions=["A descriptive completeness measure, not a quality or accuracy score."],
        coverage=1.0,
        provenance=dict(
            Counter(
                [s.provenance for s in data.suppliers]
                + [f.provenance for f in data.facilities]
                + [r.provenance for r in data.relationships]
                + [o.provenance for o in data.observations]
            )
        ),
    )


def compute_risk_summary(data: NetworkData) -> RiskSummary:
    completeness = relationship_attribute_completeness(data)
    connectivity = connectivity_coverage(data)
    obs_cov = observation_coverage(data)
    metrics = [
        single_source_facilities(data),
        supplier_dependency_concentration(data),
        completeness,
        connectivity,
        obs_cov,
        data_coverage_indicator(data, [completeness, connectivity, obs_cov]),
    ]
    return RiskSummary(metrics=metrics, disclaimer=DISCLAIMER)
