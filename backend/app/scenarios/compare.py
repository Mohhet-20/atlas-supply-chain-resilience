"""Comparison of two to five finished runs, computed from persisted results only.

Only metrics with the same definition in every run are compared. Differences in engine, configuration, traversal
assumptions or network inputs are reported as reasons a metric may not be comparable, never silently ignored.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError, NotFoundError
from app.models.enums import SimStatus
from app.models.scenario import SimulationImpact, SimulationRun

COMPARABLE_STATUSES = (SimStatus.COMPLETED, SimStatus.PARTIAL)
TRAVERSAL_KEYS = (
    "downstream_depth",
    "upstream_depth",
    "include_upstream",
    "include_ownership",
    "as_of_date",
    "include_alternatives",
    "include_proposed_alternatives",
)
MAX_ENTITY_ROWS = 200


def _events(run: SimulationRun) -> list[dict[str, Any]]:
    return [
        {
            "event_uid": e["event_uid"], "event_type": e["event_type"], "label": e.get("label"),
            "targets": sorted([f"{e['target_type']}:{e['target_id']}"] if e.get("target_id") else [f"{t['type']}:{t['id']}" for t in (e.get("targets") or [])]),
            "relationship_ref": e.get("relationship_ref"), "magnitude_pct": e.get("magnitude_pct"), "added_lead_time_days": e.get("added_lead_time_days"),
            "duration_days": e.get("duration_days"),
        }
        for e in (run.scenario_snapshot or {}).get("events", [])
    ]  # fmt: skip


def compare_runs(db: Session, run_ids: list[int]) -> dict[str, Any]:
    ids = list(dict.fromkeys(run_ids))
    if not 2 <= len(ids) <= 5:
        raise DomainValidationError("Compare between 2 and 5 distinct runs.")
    runs = {r.id: r for r in db.scalars(select(SimulationRun).where(SimulationRun.id.in_(ids)))}
    missing = [i for i in ids if i not in runs]
    if missing:
        raise NotFoundError(f"Run {missing[0]} not found.")
    bad = [r for r in runs.values() if r.status not in COMPARABLE_STATUSES]
    if bad:
        raise DomainValidationError(
            f"Run {bad[0].id} is {bad[0].status.value}; only COMPLETED or PARTIAL runs can be compared."
        )
    ordered = [runs[i] for i in ids]
    first = ordered[0]
    hard: list[dict[str, str]] = []
    soft: list[dict[str, str]] = []
    if len({r.engine_version for r in ordered}) > 1:
        hard.append(
            {
                "code": "ENGINE_VERSION_DIFFERS",
                "text": "The runs used different engine versions; results are not comparable.",
            }
        )
    if len({r.config_hash for r in ordered}) > 1:
        hard.append(
            {
                "code": "CONFIG_DIFFERS",
                "text": "The runs used different simulation configurations; results are not comparable.",
            }
        )
    traversal_diff = [k for k in TRAVERSAL_KEYS if len({str((r.assumptions or {}).get(k)) for r in ordered}) > 1]
    if traversal_diff:
        soft.append(
            {
                "code": "TRAVERSAL_ASSUMPTIONS_DIFFER",
                "text": "Traversal assumptions differ ("
                + ", ".join(traversal_diff)
                + "); entity counts and exposure sets are not like-for-like.",
            }
        )
    if len({r.graph_hash for r in ordered}) > 1:
        soft.append(
            {
                "code": "NETWORK_INPUTS_DIFFER",
                "text": "The network data the runs read was not identical (different input hash). Differences may reflect network changes as well as the scenarios.",
            }
        )
    if len({r.scenario_id for r in ordered}) == 1 and len({r.definition_hash for r in ordered}) > 1:
        soft.append(
            {
                "code": "DEFINITION_CHANGED",
                "text": "These runs are of the same scenario but different versions of its definition.",
            }
        )
    if any(r.truncated for r in ordered):
        soft.append(
            {
                "code": "TRUNCATED_RUN",
                "text": "At least one run was truncated by a limit; its affected set may be incomplete.",
            }
        )
    other_assump = sorted(
        {
            k
            for r in ordered
            for k in (r.assumptions or {})
            if k not in TRAVERSAL_KEYS and len({str((x.assumptions or {}).get(k)) for x in ordered}) > 1
        }
    )
    metrics_ok = not hard
    counts_ok = metrics_ok and not traversal_diff
    units = [set((r.summary or {}).get("capacity_at_risk_by_unit", {}).keys()) for r in ordered]
    capacity_ok = (
        metrics_ok
        and all(u == units[0] for u in units)
        and not any((r.summary or {}).get("relationships_with_unknown_capacity") for r in ordered)
    )
    rows = []
    for r in ordered:
        s = r.summary or {}
        rows.append({
            "run_id": r.id, "run_uid": r.run_uid, "scenario_id": r.scenario_id, "scenario_version": r.scenario_version, "status": r.status.value,
            "entities_affected": r.entities_affected, "direct": r.direct_count, "indirect": r.indirect_count, "by_type": s.get("by_type", {}),
            "affected_products": s.get("affected_products"), "affected_facilities": s.get("affected_facilities"),
            "affected_relationships": s.get("affected_relationships"), "capacity_at_risk_by_unit": s.get("capacity_at_risk_by_unit", {}),
            "relationships_with_unknown_capacity": s.get("relationships_with_unknown_capacity"), "alternatives_total": s.get("alternatives_total"),
            "entities_with_alternatives": s.get("entities_with_alternatives"), "by_exposure_level": s.get("by_exposure_level", {}),
            "missing_data_codes": sorted(m["code"] for m in (r.missing_data or [])), "warning_codes": sorted(w["code"] for w in (r.warnings or [])),
            "by_event": s.get("by_event", []), "events": _events(r), "truncated": r.truncated, "graph_hash": r.graph_hash,
        })  # fmt: skip
    # entity-level table
    ent: dict[tuple[str, int], dict[str, Any]] = {}
    for r in ordered:
        for i in db.scalars(select(SimulationImpact).where(SimulationImpact.run_id == r.id)):
            e = ent.setdefault(
                (i.entity_type.value, i.entity_id),
                {
                    "entity_type": i.entity_type.value,
                    "entity_id": i.entity_id,
                    "entity_name": i.entity_name,
                    "per_run": {},
                },
            )
            e["per_run"][str(r.id)] = {
                "impact_kind": i.impact_kind.value,
                "exposure_level": i.exposure_level,
                "loss_pct": i.loss_pct,
                "loss_pct_lower_bound": i.loss_pct_lower_bound,
            }
    ent_rows = sorted(ent.values(), key=lambda e: (-len(e["per_run"]), e["entity_type"], e["entity_id"]))
    in_all = sum(1 for e in ent_rows if len(e["per_run"]) == len(ordered))
    return {
        "run_ids": ids, "comparable": not hard, "not_comparable_reasons": hard, "warnings": soft,
        "metric_comparability": {"counts_and_sets": counts_ok, "capacity": capacity_ok, "events": metrics_ok},
        "assumption_differences": traversal_diff + other_assump, "runs": rows,
        "entities": {"in_all_runs": in_all, "only_in_some_runs": len(ent_rows) - in_all, "total": len(ent_rows), "rows": ent_rows[:MAX_ENTITY_ROWS], "truncated": len(ent_rows) > MAX_ENTITY_ROWS},
        "same_scenario": len({r.scenario_id for r in ordered}) == 1, "baseline_run_id": first.id,
        "note": "Comparisons use persisted results only. A historical run is never silently compared with today's network: if inputs changed, the reasons are listed above.",
    }  # fmt: skip
