"""Approved estimators (closed registry). Baselines are transparent history lookups; learned models are pure-Python
ridge / logistic regression. Nothing is loaded dynamically; every model is identified by a key from ``config``.
"""

from __future__ import annotations

from typing import Any

from app.forecast.stats import med
from app.leadtime import features as F
from app.leadtime.config import LEADTIME_BASELINES, RISK_BASELINES
from app.leadtime.dataset import Ex
from app.leadtime.fitters import SparseRow, apply_platt, dot, logistic, ridge, sigmoid

COMPLEXITY = {"global_median": 1, "base_rate": 1, "supplier_median": 2, "supplier_rate": 2, "lane_median": 3, "hierarchical_median": 4, "ridge_leadtime": 5, "logistic_late": 5}  # fmt: skip
LEARNED = ("ridge_leadtime", "logistic_late")

REGISTRY: dict[str, dict[str, Any]] = {
    "global_median": {
        "target": "LEAD_TIME",
        "kind": "baseline",
        "min_train": 5,
        "describes": "Median of all training lead times. Not supplier-specific.",
    },
    "supplier_median": {
        "target": "LEAD_TIME",
        "kind": "baseline",
        "min_train": 5,
        "describes": "Median for the supplier when it has enough training deliveries; otherwise the global median (labelled).",
    },  # fmt: skip
    "lane_median": {
        "target": "LEAD_TIME",
        "kind": "baseline",
        "min_train": 5,
        "describes": "Median for the supplier-destination lane when it has enough training deliveries; otherwise the global median (labelled).",
    },  # fmt: skip
    "hierarchical_median": {
        "target": "LEAD_TIME",
        "kind": "baseline",
        "min_train": 5,
        "describes": "Most specific of supplier+destination+product, lane, supplier, global with enough deliveries; the level used is always reported.",
    },  # fmt: skip
    "ridge_leadtime": {
        "target": "LEAD_TIME",
        "kind": "learned",
        "min_train": 30,
        "describes": "L2-regularised linear regression on prediction-point features (categoricals, quantity, calendar, point-in-time supplier/lane history). Predictions below zero are clamped and counted.",
    },  # fmt: skip
    "base_rate": {
        "target": "LATE_RISK",
        "kind": "baseline",
        "min_train": 20,
        "describes": "Training late-delivery prevalence for every delivery. A historical rate, not a per-delivery probability.",
    },  # fmt: skip
    "supplier_rate": {
        "target": "LATE_RISK",
        "kind": "baseline",
        "min_train": 20,
        "describes": "Supplier late rate shrunk toward the training prevalence. A historical rate, not a per-delivery probability.",
    },  # fmt: skip
    "logistic_late": {
        "target": "LATE_RISK",
        "kind": "learned",
        "min_train": 50,
        "describes": "L2-regularised logistic regression on prediction-point features. Output is an uncalibrated score unless held-out calibration evidence meets the documented criteria.",
    },  # fmt: skip
}
MIN_CLASS_TRAIN = 10


def eligibility(key: str, n_train: int, pos: int | None = None) -> str | None:
    r = REGISTRY[key]
    if n_train < r["min_train"]:
        return f"needs at least {r['min_train']} training examples; has {n_train}"
    if key == "logistic_late" and pos is not None and (pos < MIN_CLASS_TRAIN or n_train - pos < MIN_CLASS_TRAIN):
        return f"needs at least {MIN_CLASS_TRAIN} late and {MIN_CLASS_TRAIN} on-time training examples; has {pos} late, {n_train - pos} on time"
    return None


# --------------------------------------------------------------------------------------------------- baselines
def weighted_median(vals: list[tuple[float, float]]) -> float | None:
    """vals = [(value, weight)]; weighted median (lower of the two middle values on an exact tie)."""
    if not vals:
        return None
    s = sorted(vals)
    half = sum(w for _, w in s) / 2
    acc = 0.0
    for v, w in s:
        acc += w
        if acc >= half - 1e-12:
            return v
    return s[-1][0]


class LeadBaseline:
    """History lookup over (supplier, destination, product, value, reference-time) rows."""

    def __init__(
        self, rows: list[tuple[int | None, int, int, float, Any]], min_n: int, half_life_days: float | None, anchor: Any
    ) -> None:
        self.min_n, self.hl, self.anchor = min_n, half_life_days, anchor
        self.g: list[tuple[float, Any]] = [(r[3], r[4]) for r in rows]
        self.s: dict[Any, list[tuple[float, Any]]] = {}
        self.l: dict[Any, list[tuple[float, Any]]] = {}
        self.pl: dict[Any, list[tuple[float, Any]]] = {}
        for sup, dest, prod, y, t in rows:
            self.s.setdefault(sup, []).append((y, t))
            self.l.setdefault((sup, dest), []).append((y, t))
            self.pl.setdefault((sup, dest, prod), []).append((y, t))

    def _med(self, xs: list[tuple[float, Any]]) -> float | None:
        if self.hl:
            return weighted_median(
                [(y, 0.5 ** (max(0.0, (self.anchor - t).total_seconds() / 86400) / self.hl)) for y, t in xs]
            )
        return med([y for y, _ in xs])

    def predict(self, method: str, sup: int | None, dest: int, prod: int) -> tuple[float | None, str, int]:
        chain = {
            "global_median": [],
            "supplier_median": [("supplier", self.s.get(sup, []))],
            "lane_median": [("lane", self.l.get((sup, dest), []))],
            "hierarchical_median": [
                ("supplier+destination+product", self.pl.get((sup, dest, prod), [])),
                ("lane", self.l.get((sup, dest), [])),
                ("supplier", self.s.get(sup, [])),
            ],  # fmt: skip
        }[method]
        for level, xs in chain:
            if len(xs) >= self.min_n:
                return self._med(xs), level, len(xs)
        return self._med(self.g), "global", len(self.g)

    def values_for(self, method: str, sup: int | None, dest: int, prod: int) -> list[float]:
        """Training values behind the level a prediction would use (for conditional-on-elapsed estimates)."""
        lvl = self.predict(method, sup, dest, prod)[1]
        src = {"supplier+destination+product": self.pl.get((sup, dest, prod), []), "lane": self.l.get((sup, dest), []), "supplier": self.s.get(sup, []), "global": self.g}[lvl]  # fmt: skip
        return [y for y, _ in src]


def lead_baseline_from(train: list[Ex], min_n: int, hl: float | None) -> LeadBaseline:
    rows = [(e.d.supplier_id, e.d.destination_id, e.d.product_id, e.y, e.ref_at) for e in train]
    return LeadBaseline(rows, min_n, hl, max(e.ref_at for e in train) if train else None)


# --------------------------------------------------------------------------------------------------- learned
def raws_of(examples: list[Ex], point: str) -> list[tuple[dict[str, float | None], dict[str, str]]]:
    out = []
    for e in examples:
        v = F.view_of(e.d, point)
        out.append((F.raw_numeric(v, point, e.hf), F.raw_categorical(v, F.CATEGORICAL[point])))
    return out


def rows_of(sp: F.Spec, raws: list[tuple[dict[str, float | None], dict[str, str]]]) -> list[SparseRow]:
    return [F.transform(sp, n, c) for n, c in raws]


def fit_ridge_model(
    train: list[Ex], point: str, lam: float, max_levels: int, min_count: int = 3
) -> dict[str, Any] | None:
    raws = raws_of(train, point)
    sp = F.fit_spec(point, raws, min_count=min_count, max_levels=max_levels)
    beta = ridge(rows_of(sp, raws), [e.y for e in train], sp.dim(), lam)
    return None if beta is None else {"spec": sp, "beta": beta}


def predict_ridge(
    m: dict[str, Any], raws: list[tuple[dict[str, float | None], dict[str, str]]]
) -> tuple[list[float], int]:
    out, clamped = [], 0
    for r in rows_of(m["spec"], raws):
        v = dot(m["beta"], r)
        if v < 0:
            clamped += 1
            v = 0.0
        out.append(v)
    return out, clamped


def fit_logistic_model(
    train: list[Ex], point: str, lam: float, max_levels: int, check, min_count: int = 3
) -> dict[str, Any] | None:  # noqa: ANN001
    raws = raws_of(train, point)
    sp = F.fit_spec(point, raws, min_count=min_count, max_levels=max_levels)
    beta, info = logistic(rows_of(sp, raws), [int(e.y) for e in train], sp.dim(), max(lam, 1e-3), check=check)
    return None if beta is None else {"spec": sp, "beta": beta, "info": info}


def predict_logistic(m: dict[str, Any], raws: list[tuple[dict[str, float | None], dict[str, str]]]) -> list[float]:
    return [sigmoid(dot(m["beta"], r)) for r in rows_of(m["spec"], raws)]


class SupplierRate:
    M = 5.0

    def __init__(self, train: list[Ex]) -> None:
        self.n = len(train)
        self.p0 = sum(e.y for e in train) / self.n if self.n else 0.0
        self.t: dict[Any, list[int]] = {}
        for e in train:
            c = self.t.setdefault(e.d.supplier_id, [0, 0])
            c[0] += int(e.y)
            c[1] += 1

    def predict(self, sup: int | None) -> float:
        k, n = self.t.get(sup, [0, 0])
        return (k + self.M * self.p0) / (n + self.M)

    def to_json(self) -> dict[str, Any]:
        return {"p0": self.p0, "m": self.M, "table": {str(k): v for k, v in self.t.items()}}


__all__ = ["COMPLEXITY", "LEARNED", "REGISTRY", "LEADTIME_BASELINES", "RISK_BASELINES", "apply_platt"]
