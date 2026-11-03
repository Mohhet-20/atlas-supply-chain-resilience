"""Comparison of completed cascading analyses, from persisted results only.

The first analysis is the baseline. Entity sets and counts are compared when the algorithm versions match. Differences in
network inputs, scenario, settings or coverage are reported as context, because a change in results may reflect better
data or different settings rather than a genuine change in the modelled supply chain. Nothing is double-counted: entities
are compared by identity, and every difference points back to the source analyses.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError, NotFoundError
from app.models.enums import SimStatus
from app.models.impact import ImpactAnalysis, ImpactCriticality, ImpactFinding
from app.models.scenario import SimulationRun

CAP = 50


def _key(t: Any, i: int) -> str:
    return f"{getattr(t, 'value', t)}:{i}"


def compare_analyses(db: Session, ids: list[int]) -> dict[str, Any]:
    if len(set(ids)) != len(ids):
        raise DomainValidationError("Each analysis may be listed only once.")
    rows = {a.id: a for a in db.scalars(select(ImpactAnalysis).where(ImpactAnalysis.id.in_(ids)))}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise NotFoundError(f"Analysis not found: {missing[0]}.")
    for a in rows.values():
        if a.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
            raise DomainValidationError(
                f"Analysis {a.id} is {a.status.value}; only COMPLETED or PARTIAL analyses can be compared."
            )
    ordered = [rows[i] for i in ids]
    base = ordered[0]
    run_defs = {
        r.id: r.definition_hash
        for r in db.scalars(select(SimulationRun).where(SimulationRun.id.in_([a.run_id for a in ordered])))
    }
    findings: dict[int, dict[str, ImpactFinding]] = {a.id: {} for a in ordered}
    for f in db.scalars(
        select(ImpactFinding)
        .where(ImpactFinding.analysis_id.in_(ids))
        .order_by(ImpactFinding.analysis_id, ImpactFinding.id)
    ):
        findings[f.analysis_id][_key(f.entity_type, f.entity_id)] = f
    cands: dict[int, set[str]] = {a.id: set() for a in ordered}
    for c in db.scalars(
        select(ImpactCriticality).where(
            ImpactCriticality.analysis_id.in_(ids), ImpactCriticality.bottleneck_candidate.is_(True)
        )
    ):
        cands[c.analysis_id].add(_key(c.entity_type, c.entity_id))

    hard: list[dict[str, str]] = []
    warn: list[dict[str, str]] = []
    if len({a.algorithm_version for a in ordered}) > 1:
        hard.append(
            {
                "code": "ALGORITHM_DIFFERS",
                "text": "The analyses used different algorithm versions, so their semantics are not equivalent and results are not compared as like-for-like.",
            }
        )
    if len({a.graph_hash for a in ordered}) > 1:
        warn.append(
            {
                "code": "NETWORK_INPUTS_DIFFER",
                "text": "The network data read by these analyses differs. Differences may reflect changed data, not only the scenario.",
            }
        )
    if (
        len({run_defs.get(a.run_id) for a in ordered}) > 1
        or len({a.run_id for a in ordered}) > 1
        and len({a.scenario_id for a in ordered}) > 1
    ):
        warn.append(
            {
                "code": "SCENARIO_INPUTS_DIFFER",
                "text": "The analyses derive from different scenario definitions or scenarios.",
            }
        )
    if len({a.config_hash for a in ordered}) > 1:
        warn.append(
            {
                "code": "SETTINGS_DIFFER",
                "text": "Analysis settings (as-of date, path limits, alternatives, staleness) differ.",
            }
        )
    if any(a.truncated for a in ordered):
        warn.append(
            {"code": "TRUNCATED", "text": "At least one analysis hit a limit; its exposed set may be incomplete."}
        )
    cov = {a.id: (a.coverage or {}).get("mean_input_coverage") for a in ordered}
    covs = [v for v in cov.values() if v is not None]
    coverage_differs = bool(covs) and (max(covs) - min(covs) > 0.05)
    if coverage_differs:
        warn.append(
            {
                "code": "COVERAGE_DIFFERS",
                "text": "Input coverage differs between analyses, so changes may reflect data completeness rather than a change in the supply chain.",
            }
        )
    comparable = not hard
    runs_out = []
    for a in ordered:
        s = a.summary or {}
        runs_out.append({
            "analysis_id": a.id, "run_id": a.run_id, "scenario_id": a.scenario_id, "scenario_version": a.scenario_version, "status": a.status.value,
            "algorithm_version": a.algorithm_version, "graph_hash": a.graph_hash, "truncated": a.truncated,
            "unique_entities": s.get("unique_entities"), "initiating": s.get("initiating"), "direct": s.get("direct"), "propagated": s.get("propagated"),
            "suppliers_exposed": s.get("suppliers_exposed"), "facilities_exposed": s.get("facilities_exposed"), "products_exposed": s.get("products_exposed"),
            "quantified": s.get("quantified"), "unquantified": s.get("unquantified"), "bottleneck_candidates": s.get("bottleneck_candidates"),
            "entities_with_alternatives": s.get("entities_with_alternatives"), "mean_input_coverage": cov[a.id],
            "uncertainty_items": sum(u.get("count", 0) for u in (a.uncertainty or [])),
        })  # fmt: skip
    pairs = []
    if comparable:
        bf = findings[base.id]
        for a in ordered[1:]:
            of = findings[a.id]
            new = sorted(set(of) - set(bf))
            gone = sorted(set(bf) - set(of))
            both = sorted(set(bf) & set(of))
            depth = [
                {
                    "entity": k,
                    "baseline_depth": bf[k].min_depth,
                    "other_depth": of[k].min_depth,
                    "analysis_ids": [base.id, a.id],
                }
                for k in both
                if bf[k].min_depth != of[k].min_depth
            ]
            quant, status_changed = [], []
            for k in both:
                x, y = bf[k], of[k]
                if x.loss_pct is not None and y.loss_pct is not None:
                    if abs(x.loss_pct - y.loss_pct) > 1e-9:
                        quant.append({"entity": k, "baseline_loss_pct": x.loss_pct, "other_loss_pct": y.loss_pct, "delta_pct_points": round(y.loss_pct - x.loss_pct, 4), "analysis_ids": [base.id, a.id]})  # fmt: skip
                elif (x.loss_pct is None) != (y.loss_pct is None):
                    status_changed.append(
                        {
                            "entity": k,
                            "baseline_state": x.impact_state,
                            "other_state": y.impact_state,
                            "analysis_ids": [base.id, a.id],
                        }
                    )
            alt_b = {k for k, f in bf.items() if f.alternatives_count > 0}
            alt_o = {k for k, f in of.items() if f.alternatives_count > 0}
            pairs.append({
                "baseline_analysis_id": base.id, "other_analysis_id": a.id,
                "newly_exposed": _cap(new, a.id), "no_longer_exposed": _cap(gone, base.id), "persistently_exposed": len(both),
                "depth_changes": {"total": len(depth), "items": depth[:CAP]},
                "quantified_changes": {"total": len(quant), "items": quant[:CAP]},
                "quantification_status_changes": {"total": len(status_changed), "items": status_changed[:CAP]},
                "bottleneck_candidates": {"added": sorted(cands[a.id] - cands[base.id])[:CAP], "removed": sorted(cands[base.id] - cands[a.id])[:CAP]},
                "alternative_availability": {"gained": sorted((alt_o - alt_b) & set(of))[:CAP], "lost": sorted((alt_b - alt_o) & set(bf))[:CAP]},
                "counts": {k: (runs_out[ordered.index(a)][k] or 0) - (runs_out[0][k] or 0) for k in ("unique_entities", "facilities_exposed", "products_exposed", "suppliers_exposed", "quantified", "unquantified")},
                "coverage_delta": None if cov[a.id] is None or cov[base.id] is None else round(cov[a.id] - cov[base.id], 4),
                "uncertainty_delta": runs_out[ordered.index(a)]["uncertainty_items"] - runs_out[0]["uncertainty_items"],
                "interpretation": "Genuine change in the modelled supply chain cannot be separated from data, settings or scenario changes when any warning above is present." if warn else "No input differences were detected between these analyses, so differences reflect the modelled outcomes.",
            })  # fmt: skip
    return {
        "analysis_ids": ids, "baseline_analysis_id": base.id, "comparable": comparable, "not_comparable_reasons": hard, "warnings": warn,
        "metric_comparability": {"counts_and_sets": comparable, "quantified_values": comparable and len({a.config_hash for a in ordered}) == 1},
        "runs": runs_out, "comparisons": pairs,
        "note": "Entities are compared by identity and counted once. Quantified values are compared only for entities quantified in both analyses; units are identical by construction (% of recorded inbound supply share). Every item names its source analyses; open the finding in either analysis for paths and evidence.",
    }  # fmt: skip


def _cap(keys: list[str], analysis_id: int) -> dict[str, Any]:
    return {"total": len(keys), "items": [{"entity": k, "analysis_id": analysis_id} for k in keys[:CAP]]}
