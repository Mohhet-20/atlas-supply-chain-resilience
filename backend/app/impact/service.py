"""Output shapers for cascading impact analyses (no business logic)."""

from __future__ import annotations

from typing import Any

from app.impact import config as cfg
from app.models.enums import SimStatus
from app.models.impact import ImpactAlternative, ImpactAnalysis, ImpactCriticality, ImpactFinding

DISCLAIMER = (
    "Hypothetical, structural analysis derived from a simulation run. Potential exposure only: not a forecast, not an "
    "observed event, and not a confirmed loss. " + cfg.REACHABILITY_NOTE
)


def analysis_out(a: ImpactAnalysis, scenario_name: str | None = None, *, full: bool = False) -> dict[str, Any]:
    out = {
        "id": a.id, "analysis_uid": a.analysis_uid, "run_id": a.run_id, "scenario_id": a.scenario_id, "scenario_name": scenario_name,
        "scenario_version": a.scenario_version, "status": a.status.value, "requested_by": a.requested_by, "queued_at": a.queued_at,
        "started_at": a.started_at, "finished_at": a.finished_at, "duration_ms": a.duration_ms, "algorithm_version": a.algorithm_version,
        "source_engine_version": a.source_engine_version, "config": a.config, "config_hash": a.config_hash, "graph_hash": a.graph_hash,
        "source_run_graph_hash": a.source_run_graph_hash, "graph_basis": a.graph_basis, "cancel_requested": a.cancel_requested,
        "truncated": a.truncated, "truncation_reasons": a.truncation_reasons or [], "error": a.error,
        "complete": a.status is SimStatus.COMPLETED, "summary": a.summary, "coverage": a.coverage,
        "warnings": a.warnings or [], "failures": a.failures or [], "disclaimer": DISCLAIMER,
        "reproducibility": {
            "algorithm_version": a.algorithm_version, "config_hash": a.config_hash, "graph_hash": a.graph_hash, "run_id": a.run_id,
            "note": "Identical algorithm version, configuration and network inputs reproduce identical results. The network is referenced by hash; the relationship records used are stored with the analysis.",
        },
    }  # fmt: skip
    if full:
        out["uncertainty"] = a.uncertainty or []
        out["excluded"] = a.excluded or {}
    return out


def finding_out(f: ImpactFinding, *, detail: bool = False) -> dict[str, Any]:
    out = {
        "id": f.id, "entity_type": f.entity_type.value, "entity_id": f.entity_id, "entity_code": f.entity_code, "entity_name": f.entity_name,
        "classification": f.classification, "min_depth": f.min_depth, "quantification": f.quantification, "exposure_level": f.exposure_level,
        "impact_state": f.impact_state, "loss_pct": f.loss_pct, "loss_pct_lower_bound": f.loss_pct_lower_bound,
        "added_lead_time_days": f.added_lead_time_days, "path_count": f.path_count, "paths_complete": f.paths_complete,
        "event_uids": f.event_uids or [], "interaction": f.interaction, "confidence": f.confidence, "evidence_strength": f.evidence_strength,
        "coverage": f.coverage, "key_uncertainty": ((f.uncertainty or [{}])[0]).get("text") if f.uncertainty else None,
        "uncertainty_count": len(f.uncertainty or []), "evidence_count": len(f.evidence or []), "alternatives_count": f.alternatives_count,
        "dimension_status": {k: v.get("status") for k, v in (f.dimensions or {}).items()}, "potential_exposure": f.impact_state != "SCENARIO_ASSUMED",
    }  # fmt: skip
    if detail:
        out.update(
            paths=f.paths or [], per_event=f.per_event or [], combined=f.combined, dimensions=f.dimensions or {},
            confidence_basis=f.confidence_basis, coverage_detail=f.coverage_detail, data_quality=f.data_quality,
            uncertainty=f.uncertainty or [], evidence=f.evidence or [], assumptions=f.assumptions or [], rules=f.rules or [],
            explanation=f.explanation,
        )  # fmt: skip
    return out


def crit_out(c: ImpactCriticality) -> dict[str, Any]:
    return {
        "id": c.id, "entity_type": c.entity_type.value, "entity_id": c.entity_id, "entity_code": c.entity_code, "entity_name": c.entity_name,
        "role": c.role, "downstream_reachable": c.downstream_reachable, "products_reachable": c.products_reachable,
        "facilities_reachable": c.facilities_reachable, "paths_through": c.paths_through, "would_disconnect": c.would_disconnect,
        "would_disconnect_sample": c.would_disconnect_sample or [], "sole_known_source_of": c.sole_known_source_of or [],
        "alternatives_for_downstream": c.alternatives_for_downstream, "bottleneck_candidate": c.bottleneck_candidate,
        "reasons": c.reasons or [], "shared_upstream_of": c.shared_upstream_of or [], "caveat": c.caveat,
        "label": "Bottleneck candidate (structural, requires validation)" if c.bottleneck_candidate else "No structural bottleneck indicator",
    }  # fmt: skip


def alt_out(x: ImpactAlternative) -> dict[str, Any]:
    return {
        "id": x.id, "for_type": x.for_type.value, "for_id": x.for_id, "for_name": x.for_name, "source_type": x.source_type.value,
        "source_id": x.source_id, "source_name": x.source_name, "path": x.path or [], "path_length": x.path_length, "path_state": x.path_state,
        "feasibility": x.feasibility, "capacity_value": x.capacity_value, "capacity_unit": x.capacity_unit,
        "supply_share_pct": x.supply_share_pct, "lead_time_days": x.lead_time_days, "missing": x.missing or [], "caveat": x.caveat,
        "operationally_validated": False,
    }  # fmt: skip
