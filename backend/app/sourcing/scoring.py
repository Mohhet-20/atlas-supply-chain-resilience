"""Explainable supplier suitability score (0-100) among the ELIGIBLE candidates of one requirement.

* Metrics and direction of improvement: price (lower), lead_time (lower), reliability (higher), quality = defect rate (lower),
  capacity = coverage of the demand by the supplier's effective capacity (higher, capped at 1), risk (lower).
* Each metric is min-max normalised across the eligible candidates to [0, 1] where 1 is best; if every candidate has the same
  value the metric does not discriminate and every candidate gets 1.
* A candidate with no value for a metric gets 0 for it (never a favourable default); the ranking is flagged incomplete and the
  weight share that had data is reported as ``weight_coverage``.
* Price uses purchase price plus freight only when every eligible candidate states freight; otherwise purchase price alone, so a
  freight charge is never counted for some candidates and ignored for others.
* Weights are validated and rescaled to sum to 1. Reliability and supplier risk can describe the same delivery uncertainty, so
  weighting both raises a warning rather than silently counting it twice.
* Ties break by higher score, then lower price, then supplier code, so a fixed snapshot always gives the same order.
The score is a comparison aid. It is not a probability of success and it is never written back to a supplier record.
"""

from __future__ import annotations

from typing import Any

from app.sourcing.config import DIRECTION, METRICS, Weights
from app.sourcing.model import Candidate


def raw_metrics(c: Candidate, demand: float, use_freight: bool) -> dict[str, float | None]:
    price = c.price
    if price is not None and use_freight and c.freight is not None:
        price = price + c.freight
    cap = c.effective_capacity
    return {
        "price": price,
        "lead_time": c.lead_days,
        "reliability": c.reliability.value,
        "quality": c.defect_rate,
        "capacity": None if cap is None else min(1.0, cap / demand),
        "risk": c.risk,
    }


def score_all(
    cands: list[Candidate], weights: Weights, demand: float
) -> tuple[dict[str, dict[str, Any]], list[str], str]:
    w = weights.normalised()
    warnings: list[str] = []
    if w["reliability"] > 0 and w["risk"] > 0:
        warnings.append(
            "Both delivery reliability and supplier risk are weighted. A supplier's risk assessment can already reflect delivery "
            "performance, so these two components may partly count the same concern twice."
        )
    use_freight = bool(cands) and all(c.freight is not None and c.price is not None for c in cands)
    basis = "PURCHASE_PLUS_FREIGHT" if use_freight else "PURCHASE_ONLY"
    raws = {c.uid: raw_metrics(c, demand, use_freight) for c in cands}
    ranges: dict[str, tuple[float, float] | None] = {}
    for m in METRICS:
        vals = [r[m] for r in raws.values() if r[m] is not None]
        ranges[m] = (min(vals), max(vals)) if vals else None
    out: dict[str, dict[str, Any]] = {}
    for c in cands:
        comps: list[dict[str, Any]] = []
        total = 0.0
        covered = 0.0
        missing: list[str] = []
        for m in METRICS:
            v = raws[c.uid][m]
            rng = ranges[m]
            if w[m] == 0:
                continue
            if v is None or rng is None:
                norm = 0.0
                missing.append(m)
            else:
                lo, hi = rng
                norm = 1.0 if hi == lo else ((hi - v) / (hi - lo) if DIRECTION[m] == "lower" else (v - lo) / (hi - lo))
                covered += w[m]
            contrib = 100.0 * w[m] * norm
            total += contrib
            comps.append({"metric": m, "raw": v, "normalised": round(norm, 6), "weight": round(w[m], 6), "contribution": round(contrib, 4), "direction": DIRECTION[m], "available": v is not None})  # fmt: skip
        out[c.uid] = {"score": round(total, 4), "components": comps, "missing_metrics": missing, "complete": not missing, "weight_coverage": round(covered, 6)}  # fmt: skip
    return out, warnings, basis


def rank_key(c: Candidate, score: float) -> tuple[Any, ...]:
    return (-score, c.price if c.price is not None else float("inf"), c.supplier_code)


def rank(cands: list[Candidate], scores: dict[str, dict[str, Any]]) -> list[str]:
    return [c.uid for c in sorted(cands, key=lambda c: rank_key(c, scores[c.uid]["score"]))]
