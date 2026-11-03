"""Pure comparison, aggregation and decision-alternative logic over a frozen evidence snapshot.

``evaluate(snapshot)`` depends only on the snapshot, so replaying a stored snapshot reproduces the stored result exactly. Rules enforced here:
 - exactly one BASELINE plan; every other plan is compared with it under the same condition (falling back, with a note, to the same-named condition in another
   mode, then to normal conditions);
 - a metric is compared only when unit, currency, cost basis and (for scope-dependent metrics) product, destination, quantity and unit agree;
 - a zero baseline gives no percentage; a missing value is reported as missing, never zero;
 - a response that improves some metrics and worsens others is a TRADE_OFF, never a winner;
 - a probability-weighted value exists only if every disrupted condition of the plan has a probability, they sum to 1 and a basis is stated.
"""

from __future__ import annotations

import math
from typing import Any

from app.decision import metrics as M
from app.decision.config import ALGORITHM_VERSION, MAINTAIN_BASELINE, METRIC_REGISTRY_VERSION, STATEMENTS, stable_hash
from app.decision.evidence import NORMAL, Evidence

SCOPE_KEYS = ("product_id", "destination_facility_id", "quantity", "unit", "currency")
Q_KEYS = ("currency", "basis")


def assemble(evs: list[Evidence], definition: dict[str, Any]) -> dict[str, Any]:
    plans: dict[str, dict[str, Any]] = {}
    conds: dict[str, dict[str, Any]] = {NORMAL: {"key": NORMAL, "name": NORMAL, "mode": "NORMAL", "probability": None}}
    cells: dict[str, Any] = {}
    exposure: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    issues: list[dict[str, str]] = []
    for ev in evs:
        for p in ev.plans:
            if p["key"] in plans:
                issues.append({"code": "DUPLICATE_PLAN", "text": f"Plan {p['key']} appears twice."})
            plans[p["key"]] = p
        for c in ev.conditions:
            prev = conds.get(c["key"])
            if prev is None:
                conds[c["key"]] = dict(c)
            elif c.get("probability") is not None:
                if prev.get("probability") not in (None, c["probability"]):
                    issues.append(
                        {
                            "code": "PROBABILITY_CONFLICT",
                            "text": f"Condition '{c['key']}' has two different probabilities in the attached runs.",
                        }
                    )
                prev["probability"] = c["probability"]
        cells.update(ev.cells)
        exposure.extend(ev.exposure)
        warnings.extend(ev.warnings)
    for pr in definition.get("probabilities", []):
        hit = [c for c in conds.values() if c["name"] == pr["condition"] and c["key"] != NORMAL]
        if not hit:
            warnings.append(
                {
                    "code": "UNKNOWN_CONDITION",
                    "text": f"A probability was given for '{pr['condition']}', which no attached run contains.",
                }
            )
        for c in hit:
            c["probability"] = pr["probability"]
    return {
        "algorithm_version": ALGORITHM_VERSION, "metric_registry_version": METRIC_REGISTRY_VERSION, "horizon_days": definition.get("horizon_days"),
        "assumptions": definition.get("assumptions", []), "scenario_ids": definition.get("scenario_ids", []), "plans": list(plans.values()), "conditions": list(conds.values()),
        "cells": cells, "exposure": exposure, "probability_basis": definition.get("probability_basis"), "evidence": [{"ref": e.ref, "source": e.source} for e in evs],
        "warnings": warnings, "issues": issues,
    }  # fmt: skip


def _lookup(base_cells: dict[str, Any], conds: dict[str, dict[str, Any]], key: str) -> tuple[str | None, str | None]:
    if key in base_cells:
        return key, None
    name = conds[key]["name"]
    for k, c in conds.items():
        if k in base_cells and c["name"] == name and k != NORMAL:
            return (
                k,
                f"the baseline was evaluated for '{name}' in a different mode ({c['mode']}); the two modes are not like-for-like",
            )
    if NORMAL in base_cells:
        return (
            NORMAL,
            "no baseline value exists under this condition; compared with the baseline under normal conditions, so the change mixes the disruption and the plan",
        )
    return None, None


def _row(
    mid: str, cond: str, ref_cond: str | None, b: dict[str, Any] | None, v: dict[str, Any] | None, scope_diff: list[str]
) -> dict[str, Any]:
    meta = M.BY_ID[mid]
    row: dict[str, Any] = {"metric": mid, "condition": cond, "reference_condition": ref_cond, "baseline": None if b is None else b["value"], "value": None if v is None else v["value"],
                           "unit": (v or b or {}).get("unit"), "direction": meta["direction"], "difference": None, "relative_pct": None, "relative_note": None, "verdict": "unknown", "status": "comparable", "reason": None}  # fmt: skip
    if v is None:
        row.update(status="missing_value", reason="the plan's run does not state this metric")
        return row
    if b is None:
        row.update(status="missing_baseline", reason="the baseline's run does not state this metric")
        return row
    bad = [k for k in Q_KEYS if b.get(k) != v.get(k)]
    if b.get("unit") != v.get("unit"):
        bad.insert(0, "unit")
    if bad:
        row.update(
            status="incomparable",
            reason=f"{', '.join(bad)} differ ({', '.join(str(b.get(k)) + ' vs ' + str(v.get(k)) for k in bad)})",
        )
        return row
    if meta["scope_dependent"] and scope_diff:
        row.update(
            status="incomparable", reason=f"the plans answer different requirements ({', '.join(scope_diff)} differ)"
        )
        return row
    diff = v["value"] - b["value"]
    row["difference"] = diff
    if b["value"] == 0:
        row["relative_note"] = "the baseline is 0, so a percentage is undefined"
    else:
        row["relative_pct"] = 100.0 * diff / abs(b["value"])
    row["verdict"] = M.verdict(mid, b["value"], v["value"])
    return row


def _classify(counts: dict[str, int]) -> str:
    if counts["comparable"] == 0:
        return "NOT_COMPARABLE"
    if counts["better"] and counts["worse"]:
        return "TRADE_OFF"
    if counts["worse"]:
        return "WORSE_ON_COMPARABLE_METRICS"
    if counts["better"]:
        return "BETTER_ON_COMPARABLE_METRICS"
    return "NO_DIFFERENCE"


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    c = {"better": 0, "worse": 0, "same": 0, "changed": 0, "comparable": 0, "incomparable": 0, "missing": 0}
    for r in rows:
        if r["status"] == "comparable":
            c["comparable"] += 1
            c[r["verdict"]] += 1
        elif r["status"] == "incomparable":
            c["incomparable"] += 1
        else:
            c["missing"] += 1
    return c


def _aggregate(
    plan: dict[str, Any], cells: dict[str, Any], conds: dict[str, dict[str, Any]], basis: str | None
) -> dict[str, Any]:
    disrupted = [k for k in conds if k != NORMAL and k in cells]
    out: list[dict[str, Any]] = []
    probs = [conds[k].get("probability") for k in disrupted]
    reason = None
    if not disrupted:
        reason = "the plan was not evaluated under any disrupted condition"
    elif any(p is None for p in probs):
        reason = "at least one disrupted condition has no probability; the values are shown per condition, as a range and a worst case"
    elif abs(sum(probs) - 1.0) > 1e-6:
        reason = f"the probabilities sum to {sum(probs):.6g}, not 1"
    elif not basis:
        reason = "no probability basis (expert-defined, historical or model estimate) was stated"
    for mid in M.ORDER:
        have = [(k, cells[k][mid]) for k in disrupted if mid in cells[k]]
        if not have:
            continue
        meta = M.BY_ID[mid]
        vals = [(k, c["value"]) for k, c in have]
        lo, hi = min(vals, key=lambda x: x[1]), max(vals, key=lambda x: x[1])
        worst, best = (
            (hi, lo) if meta["direction"] == "min" else (lo, hi) if meta["direction"] == "max" else (None, None)
        )
        e: dict[str, Any] = {"metric": mid, "unit": have[0][1].get("unit"), "n_conditions": len(vals), "of_conditions": len(disrupted), "min": {"value": lo[1], "condition": lo[0]}, "max": {"value": hi[1], "condition": hi[0]},
                             "worst": None if worst is None else {"value": worst[1], "condition": worst[0]}, "best": None if best is None else {"value": best[1], "condition": best[0]}, "expected": None, "expected_reason": reason}  # fmt: skip
        if (
            reason is None
            and len(vals) == len(disrupted)
            and all(c.get("currency") == have[0][1].get("currency") for _, c in have)
        ):
            e["expected"] = {"value": sum(conds[k]["probability"] * v for k, v in vals), "basis": basis}
        elif reason is None:
            e["expected_reason"] = "the metric is missing under some disrupted conditions"
        out.append(e)
    return {
        "plan_key": plan["key"],
        "weighted": any(m["expected"] for m in out),
        "weighting_reason": reason,
        "probability_basis": basis if reason is None else None,
        "metrics": out,
    }


def _describe(rows: list[dict[str, Any]], verdict: str) -> list[str]:
    return [M.BY_ID[r["metric"]]["name"] for r in rows if r["status"] == "comparable" and r["verdict"] == verdict]


def evaluate(snap: dict[str, Any]) -> dict[str, Any]:
    conds = {c["key"]: c for c in snap["conditions"]}
    cells = snap["cells"]
    warnings = list(snap.get("warnings", []))
    issues = list(snap.get("issues", []))
    base = [p for p in snap["plans"] if p["role"] == "BASELINE"]
    others = [p for p in snap["plans"] if p["role"] != "BASELINE"]
    result: dict[str, Any] = {"algorithm_version": ALGORITHM_VERSION, "metric_registry_version": METRIC_REGISTRY_VERSION, "baseline": None, "conditions": snap["conditions"], "comparisons": [],
                              "aggregation": [], "alternatives": [], "exposure": snap["exposure"], "warnings": warnings, "issues": issues, "unavailable_metrics": M.UNAVAILABLE, "statements": STATEMENTS}  # fmt: skip
    if issues:
        result["result_status"] = "invalid_input"
        return _finish(result)
    if len(base) != 1:
        issues.append({"code": "BASELINE_REQUIRED" if not base else "MULTIPLE_BASELINES", "text": ("No plan is designated BASELINE. Attach the run whose plan represents the reference state and give it the role BASELINE." if not base else "More than one plan is designated BASELINE; exactly one reference is allowed.")})  # fmt: skip
        result["result_status"] = "insufficient_data" if not base else "invalid_input"
        return _finish(result)
    b = base[0]
    result["baseline"] = {k: b[k] for k in ("key", "label", "source", "scope", "allocation", "reference_kind")}
    if b.get("reference_kind") == "REFERENCE_COST_MINIMAL":
        warnings.append(
            {
                "code": "BASELINE_IS_REFERENCE",
                "text": "The baseline is a computed cost-minimal reference plan, not the current operation. Do not read differences as changes from today's practice.",
            }
        )
    if not others and not snap["exposure"]:
        issues.append(
            {
                "code": "NOTHING_TO_COMPARE",
                "text": "Only the baseline was attached. Add an alternative plan or a scenario exposure run.",
            }
        )
        result["result_status"] = "insufficient_data"
        return _finish(result)
    bcells = cells.get(b["key"], {})
    for p in others:
        pcells = cells.get(p["key"], {})
        scope_diff = [k for k in SCOPE_KEYS if (p["scope"] or {}).get(k) != (b["scope"] or {}).get(k)]
        comps, allrows = [], []
        for ck in [c["key"] for c in snap["conditions"] if c["key"] in pcells]:
            rk, note = _lookup(bcells, conds, ck)
            bc = bcells.get(rk, {}) if rk else {}
            rows = [
                _row(mid, ck, rk, bc.get(mid), pcells[ck].get(mid), scope_diff)
                for mid in M.ORDER
                if mid in pcells[ck] or mid in bc
            ]
            rows = [r for r in rows if r["status"] != "missing_value" or r["baseline"] is not None]
            allrows += rows
            comps.append(
                {
                    "condition": ck,
                    "reference_condition": rk,
                    "reference_note": note,
                    "rows": rows,
                    "counts": _counts(rows),
                    "classification": _classify(_counts(rows)),
                }
            )
        total = _counts(allrows)
        result["comparisons"].append(
            {
                "plan_key": p["key"],
                "label": p["label"],
                "role": p["role"],
                "scope_differences": scope_diff,
                "conditions": comps,
                "counts": total,
                "classification": _classify(total),
            }
        )
        result["aggregation"].append(_aggregate(p, pcells, conds, snap.get("probability_basis")))
        normal = next((c for c in comps if c["condition"] == NORMAL), None)
        nrows = normal["rows"] if normal else []
        improves, worsens = _describe(nrows, "better"), _describe(nrows, "worse")
        scen_better = [
            c["condition"]
            for c in comps
            if c["condition"] != NORMAL
            and any(
                r["status"] == "comparable"
                and r["verdict"] == "better"
                and r["metric"] in ("UNMET_UNITS", "FULFILMENT")
                for r in c["rows"]
            )
        ]
        scen_worse = [
            c["condition"]
            for c in comps
            if c["condition"] != NORMAL
            and any(
                r["status"] == "comparable" and r["verdict"] == "worse" and r["metric"] in ("UNMET_UNITS", "FULFILMENT")
                for r in c["rows"]
            )
        ]
        bsupp = {a["supplier"] for a in b.get("allocation", [])}
        psupp = {a["supplier"] for a in p.get("allocation", [])}
        failed = [c["id"] for c in p.get("constraint_checks", []) if not c["passed"]]
        unc = [
            w["text"]
            for w in warnings
            if w["code"] in ("SEARCH_LIMITED", "REOPTIMISED_UNDER_SCENARIO", "RUN_HAD_WARNINGS")
            and p["source"] in w["text"]
        ]
        miss = sorted(
            {r["metric"] for c in comps for r in c["rows"] if r["status"] in ("missing_baseline", "missing_value")}
        )
        if miss:
            unc.append(
                "Not comparable because one side does not state them: "
                + ", ".join(M.BY_ID[m]["name"] for m in miss)
                + "."
            )
        if scope_diff:
            unc.append(
                "This plan answers a different requirement than the baseline ("
                + ", ".join(scope_diff)
                + "); scope-dependent metrics are not compared."
            )
        if any(c["reference_note"] for c in comps):
            unc.append("Some conditions had no baseline value under the same condition; see the comparison notes.")
        if total["incomparable"]:
            unc.append(
                f"{total['incomparable']} metric value(s) were not compared because units, currency or cost basis differ."
            )
        cost_notes = [f"Modelled cost changes by {r['difference']:+.2f} {((r['unit']) or '')}".strip() + (f" ({r['relative_pct']:+.1f}%)" if r["relative_pct"] is not None else "") + "." for r in nrows if r["metric"] == "MODELLED_COST" and r["status"] == "comparable" and r["verdict"] in ("better", "worse")]  # fmt: skip
        text = {"TRADE_OFF": "Improves " + ", ".join(improves) + "; worsens " + ", ".join(worsens) + ". Whether that balance is acceptable is a judgement for the reviewer.",
                "BETTER_ON_COMPARABLE_METRICS": "Improves " + ", ".join(improves or ["a comparable metric under a disruption"]) + " and worsens no comparable metric.",
                "WORSE_ON_COMPARABLE_METRICS": "Worsens " + ", ".join(worsens or ["a comparable metric under a disruption"]) + " and improves no comparable metric.",
                "NO_DIFFERENCE": "No comparable metric differs from the baseline.", "NOT_COMPARABLE": "Nothing could be compared with the baseline."}[_classify(total)]  # fmt: skip
        result["alternatives"].append({
            "key": p["key"], "label": p["label"], "source": p["source"], "classification": _classify(total), "improves": improves, "worsens": worsens,
            "scenarios_improved": scen_better, "scenarios_worsened": scen_worse, "constraints": {"all_passed": not failed and bool(p.get("constraint_checks")), "failed": failed, "checked": len(p.get("constraint_checks", []))},
            "feasible": p.get("feasible"), "allocation": p.get("allocation"), "prerequisites": (["Uses supplier(s) the baseline does not: " + ", ".join(sorted(s for s in psupp - bsupp if s))] if psupp - bsupp else []) + (["Stops using supplier(s) the baseline uses: " + ", ".join(sorted(s for s in bsupp - psupp if s))] if bsupp - psupp else []) + ["Approving records a decision only; any purchase, contract or allocation change must be made separately by the responsible team."],  # fmt: skip
            "uncertainties": unc, "suitability": text, "cost_notes": cost_notes, "counts": total,
        })  # fmt: skip
    bn = bcells.get(NORMAL, {})
    result["alternatives"].insert(0, {
        "key": MAINTAIN_BASELINE, "label": "Maintain the baseline plan", "source": b["source"], "classification": "BASELINE", "improves": [], "worsens": [], "scenarios_improved": [], "scenarios_worsened": [],
        "constraints": {"all_passed": not [c for c in b.get("constraint_checks", []) if not c["passed"]] and bool(b.get("constraint_checks")), "failed": [c["id"] for c in b.get("constraint_checks", []) if not c["passed"]], "checked": len(b.get("constraint_checks", []))},
        "feasible": b.get("feasible"), "allocation": b.get("allocation"), "prerequisites": ["No change to current practice is implied by selecting this."], "uncertainties": [w["text"] for w in warnings if w["code"] == "BASELINE_IS_REFERENCE"],
        "suitability": "Keeps the reference plan. Its own outcomes under each condition are in the comparison table.", "cost_notes": [], "counts": None, "metrics_normal": {k: v["value"] for k, v in bn.items()},
    })  # fmt: skip
    result["result_status"] = "completed_with_warnings" if warnings else "completed"
    return _finish(result)


def _finish(result: dict[str, Any]) -> dict[str, Any]:
    result["result_hash"] = stable_hash({k: v for k, v in result.items() if k != "result_hash"})
    return result


def cost_hidden(result: dict[str, Any]) -> dict[str, Any]:
    """A viewer's copy: every cost value, cost sentence and cost-derived percentage is removed; the verdict words stay."""
    import copy

    out = copy.deepcopy(result)
    for comp in out["comparisons"]:
        for c in comp["conditions"]:
            for r in c["rows"]:
                if r["metric"] in M.COST_IDS:
                    r.update(
                        baseline=None, value=None, difference=None, relative_pct=None, relative_note=None, hidden=True
                    )
    for a in out["alternatives"]:
        a["cost_notes"] = []
        if "metrics_normal" in a:
            a["metrics_normal"] = {k: v for k, v in a["metrics_normal"].items() if k not in M.COST_IDS}
    for ag in out["aggregation"]:
        for m in ag["metrics"]:
            if m["metric"] in M.COST_IDS:
                m.update(min=None, max=None, worst=None, best=None, expected=None, hidden=True)
    out["cost_redacted"] = True
    return out


def is_finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
