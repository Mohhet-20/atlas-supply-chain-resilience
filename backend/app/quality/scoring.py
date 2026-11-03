"""Transparent, reproducible quality scoring. See docs/methodology/data-quality-scoring.md.

score = 100 x (1 - failed_weight / applicable_weight)

* A *unit* is one rule applied to one field (several instances of the same rule on one field-group, such as several
  duplicate candidates, count once, so one underlying issue is not counted repeatedly).
* ``applicable_weight`` sums the severity weight of every weighted unit that was evaluated; ``failed_weight`` sums the
  weights of failed units. INFO rules weigh 0: they are reported but never change a score.
* Accepted exceptions are not counted as failed (they are listed in ``exceptions_excluded``).
* Fewer than ``MIN_WEIGHTED_UNITS`` weighted units -> INSUFFICIENT_EVIDENCE and no number.
* A high score never means the data is correct: it only means few of the checks that exist found a problem.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.models.enums import ScoreState
from app.quality.framework import Check, RuleSpec

FORMULA_VERSION = "qs-1"
MIN_WEIGHTED_UNITS = 3
BUCKETS = (
    ("below_50", 0.0, 50.0),
    ("50_to_70", 50.0, 70.0),
    ("70_to_90", 70.0, 90.0),
    ("90_and_above", 90.0, 100.0001),
)


@dataclass(frozen=True)
class Outcome:
    rule: RuleSpec
    check: Check
    excepted: bool = False


@dataclass(frozen=True)
class ScoreResult:
    state: ScoreState
    score: float | None
    checks_total: int
    checks_failed: int
    exceptions_excluded: int
    applicable_weight: float
    failed_weight: float
    breakdown: dict[str, Any]


def _unit(o: Outcome) -> tuple[str, str]:
    base = (o.check.field or "").split(":")[0]
    return o.rule.rule_id, base


def score_subject(outcomes: list[Outcome]) -> ScoreResult:
    units: dict[tuple[str, str], list[Outcome]] = defaultdict(list)
    for o in outcomes:
        units[_unit(o)].append(o)
    dims: dict[str, dict[str, Any]] = {}
    applicable = failed_w = 0.0
    n_units = n_failed = n_excepted = weighted = 0
    info_failed = missing = invalid = 0
    for group in units.values():
        rule = group[0].rule
        failing = [o for o in group if not o.check.passed]
        real = [o for o in failing if not o.excepted]
        d = dims.setdefault(
            rule.dimension.value, {"units": 0, "failed": 0, "applicable_weight": 0.0, "failed_weight": 0.0}
        )
        if rule.weight == 0:
            info_failed += 1 if real else 0
            continue
        weighted += 1
        n_units += 1
        applicable += rule.weight
        d["units"] += 1
        d["applicable_weight"] += rule.weight
        if real:
            n_failed += 1
            failed_w += rule.weight
            d["failed"] += 1
            d["failed_weight"] += rule.weight
            if rule.dimension.value == "completeness":
                missing += 1
            if rule.dimension.value == "validity":
                invalid += 1
        elif failing:
            n_excepted += 1
    for d in dims.values():
        aw = d["applicable_weight"]
        d["score"] = round(100.0 * (1 - d["failed_weight"] / aw), 1) if aw else None
    breakdown = {"dimensions": dims, "missing_units": missing, "invalid_units": invalid, "info_findings": info_failed}
    if weighted < MIN_WEIGHTED_UNITS or applicable <= 0:
        return ScoreResult(
            ScoreState.INSUFFICIENT_EVIDENCE, None, n_units, n_failed, n_excepted, applicable, failed_w, breakdown
        )
    return ScoreResult(
        ScoreState.SCORED,
        round(100.0 * (1 - failed_w / applicable), 1),
        n_units,
        n_failed,
        n_excepted,
        applicable,
        failed_w,
        breakdown,
    )


def aggregate(scores: list[float | None], total_subjects: int) -> dict[str, Any]:
    """Mean over SCORED subjects only. Reports coverage so a mean of three records is never mistaken for a network score."""
    scored = [s for s in scores if s is not None]
    out: dict[str, Any] = {
        "subjects": total_subjects,
        "scored": len(scored),
        "insufficient_evidence": len(scores) - len(scored),
        "not_evaluated": max(0, total_subjects - len(scores)),
        "coverage": round(len(scored) / total_subjects, 4) if total_subjects else None,
        "mean": None,
        "min": None,
        "distribution": None,
        "state": "UNAVAILABLE",
        "formula_version": FORMULA_VERSION,
    }
    if scored:
        out["mean"] = round(sum(scored) / len(scored), 1)
        out["min"] = min(scored)
        out["distribution"] = {name: sum(1 for s in scored if lo <= s < hi) for name, lo, hi in BUCKETS}
        out["state"] = "SCORED"
    return out
