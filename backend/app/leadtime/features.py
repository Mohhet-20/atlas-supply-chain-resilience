"""Prediction-point feature views and the design matrix. Leakage is prevented by construction.

A feature function only receives a *view* containing the fields that exist at the prediction point; arrival, receipt,
cancellation, the latest (possibly revised) promise and anything after the point are simply not in the view, so reading
them raises KeyError. Preprocessing (levels, scaling, imputation values) is fitted on the training split only and stored.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.leadtime.fitters import SparseRow
from app.leadtime.history import HF
from app.leadtime.semantics import Delivery

POINT_FIELDS: dict[str, tuple[str, ...]] = {
    "ORDER_PLACEMENT": ("order_at", "quantity", "supplier_id", "destination_id", "product_id", "origin_id"),
    "CONFIRMATION": (
        "order_at",
        "quantity",
        "supplier_id",
        "destination_id",
        "product_id",
        "origin_id",
        "confirmed_at",
        "original_promised_at",
        "promised_dispatch_at",
    ),  # fmt: skip
    "DISPATCH": (
        "order_at",
        "quantity",
        "supplier_id",
        "destination_id",
        "product_id",
        "origin_id",
        "confirmed_at",
        "original_promised_at",
        "promised_dispatch_at",
        "dispatched_at",
        "mode",
    ),  # fmt: skip
}
REF_FIELD = {"ORDER_PLACEMENT": "order_at", "CONFIRMATION": "confirmed_at", "DISPATCH": "dispatched_at"}
CATEGORICAL = {"ORDER_PLACEMENT": ("supplier", "destination", "product"), "CONFIRMATION": ("supplier", "destination", "product"), "DISPATCH": ("supplier", "destination", "product", "mode")}  # fmt: skip
FORBIDDEN_EVERYWHERE = (
    "arrived_at",
    "received_at",
    "cancelled_at",
    "latest_promised_at",
    "received_quantity",
    "reported_status",
)


def view_of(d: Delivery, point: str) -> dict[str, Any]:
    return {f: getattr(d, f) for f in POINT_FIELDS[point]}


def reference_time(d: Delivery, point: str) -> datetime | None:
    return getattr(d, REF_FIELD[point])


def numeric_names(point: str) -> list[str]:
    base = ["log_qty", "month_sin", "month_cos", "weekday_sin", "weekday_cos", "hist_sup_logn", "hist_sup_median", "hist_sup_late", "hist_lane_logn", "hist_lane_median"]  # fmt: skip
    if point in ("CONFIRMATION", "DISPATCH"):
        base += ["confirm_delay_days", "promised_lead_days"]
    if point == "DISPATCH":
        base += ["dispatch_delay_days", "days_to_promise", "order_to_dispatch_days"]
    return base


def _days(a: datetime | None, b: datetime | None) -> float | None:
    return None if a is None or b is None else (b - a).total_seconds() / 86400.0


def raw_numeric(view: dict[str, Any], point: str, hf: HF | None) -> dict[str, float | None]:
    t = view[REF_FIELD[point]]
    out: dict[str, float | None] = {
        "log_qty": math.log1p(view["quantity"]) if view["quantity"] else None,
        "month_sin": math.sin(2 * math.pi * (t.month - 1) / 12), "month_cos": math.cos(2 * math.pi * (t.month - 1) / 12),
        "weekday_sin": math.sin(2 * math.pi * t.weekday() / 7), "weekday_cos": math.cos(2 * math.pi * t.weekday() / 7),
        "hist_sup_logn": math.log1p(hf.sup_n) if hf else None, "hist_sup_median": hf.sup_median if hf else None,
        "hist_sup_late": hf.sup_late_rate if hf else None, "hist_lane_logn": math.log1p(hf.lane_n) if hf else None,
        "hist_lane_median": hf.lane_median if hf else None,
    }  # fmt: skip
    if point in ("CONFIRMATION", "DISPATCH"):
        out["confirm_delay_days"] = _days(view["order_at"], view["confirmed_at"])
        out["promised_lead_days"] = _days(view["confirmed_at"], view["original_promised_at"])
    if point == "DISPATCH":
        out["dispatch_delay_days"] = _days(view["promised_dispatch_at"], view["dispatched_at"])
        out["days_to_promise"] = _days(view["dispatched_at"], view["original_promised_at"])
        out["order_to_dispatch_days"] = _days(view["order_at"], view["dispatched_at"])
    return out


def raw_categorical(view: dict[str, Any], names: tuple[str, ...]) -> dict[str, str]:
    m = {
        "supplier": view["supplier_id"],
        "destination": view["destination_id"],
        "product": view["product_id"],
        "mode": view.get("mode"),
    }
    return {n: ("UNKNOWN" if m[n] is None else str(m[n])) for n in names}


@dataclass
class Spec:
    point: str
    cat_levels: dict[str, list[str]] = field(default_factory=dict)
    num_names: list[str] = field(default_factory=list)
    num_mean: dict[str, float] = field(default_factory=dict)
    num_std: dict[str, float] = field(default_factory=dict)
    miss_flag: list[str] = field(
        default_factory=list
    )  # numeric features that had missing values in training get an indicator
    columns: list[str] = field(default_factory=lambda: ["intercept"])

    def dim(self) -> int:
        return len(self.columns)

    def to_json(self) -> dict[str, Any]:
        return {"point": self.point, "cat_levels": self.cat_levels, "num_names": self.num_names, "num_mean": self.num_mean, "num_std": self.num_std, "miss_flag": self.miss_flag, "columns": self.columns}  # fmt: skip

    @staticmethod
    def from_json(j: dict[str, Any]) -> Spec:
        return Spec(
            j["point"], j["cat_levels"], j["num_names"], j["num_mean"], j["num_std"], j["miss_flag"], j["columns"]
        )


def fit_spec(
    point: str, raws: list[tuple[dict[str, float | None], dict[str, str]]], *, min_count: int, max_levels: int
) -> Spec:
    """Fit levels, scaling and imputation on TRAINING rows only."""
    sp = Spec(point=point, num_names=numeric_names(point))
    cols = ["intercept"]
    for cat in CATEGORICAL[point]:
        cnt = Counter(c[cat] for _, c in raws)
        keep = sorted([lv for lv, k in cnt.most_common(max_levels) if k >= min_count])
        sp.cat_levels[cat] = keep
        if len(keep) >= 1:
            cols += [f"{cat}={lv}" for lv in keep]
            cols.append(f"{cat}=other")
    for nm in sp.num_names:
        vals = [r[nm] for r, _ in raws if r.get(nm) is not None]
        miss = len(raws) - len(vals)
        if len(vals) < 2:
            sp.num_mean[nm], sp.num_std[nm] = 0.0, 0.0  # constant: contributes nothing
        else:
            m = sum(vals) / len(vals)
            s = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
            sp.num_mean[nm], sp.num_std[nm] = m, s
        if miss and vals:
            sp.miss_flag.append(nm)
        cols.append(nm)
    cols += [f"missing:{nm}" for nm in sp.miss_flag]
    sp.columns = cols
    return sp


def transform(sp: Spec, num: dict[str, float | None], cat: dict[str, str]) -> SparseRow:
    row: SparseRow = [(0, 1.0)]
    idx = {c: i for i, c in enumerate(sp.columns)}
    for c, levels in sp.cat_levels.items():
        if not levels:
            continue
        lv = cat[c]
        row.append((idx[f"{c}={lv}"] if lv in levels else idx[f"{c}=other"], 1.0))
    for nm in sp.num_names:
        v = num.get(nm)
        s = sp.num_std[nm]
        if s > 1e-12 and v is not None:
            row.append((idx[nm], (v - sp.num_mean[nm]) / s))
        if nm in sp.miss_flag and v is None:
            row.append((idx[f"missing:{nm}"], 1.0))
    return row


def contributions(sp: Spec, beta: list[float], row: SparseRow) -> list[tuple[str, float]]:
    """Linear contribution of each active column (coefficient x standardised value). Not a causal effect."""
    return sorted(((sp.columns[i], beta[i] * v) for i, v in row if i != 0), key=lambda kv: -abs(kv[1]))


def group_of(col: str) -> str:
    if col == "intercept":
        return "intercept"
    if col.startswith("missing:"):
        return col[len("missing:") :]
    return col.split("=")[0]
