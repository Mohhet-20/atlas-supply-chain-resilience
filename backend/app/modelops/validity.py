"""Leakage and validity checks. Each check returns ``{"check", "status", "detail"}``; status is pass | fail | not_checked.

``fail`` makes an evaluation ``invalid`` (it is stored with the failure, never silently repaired). ``not_checked`` is used when the
stored run does not carry the information needed; it is shown as a limitation rather than treated as a pass.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence
from datetime import datetime
from typing import Any


def _c(check: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"check": check, "status": status, "detail": detail, **extra}


def temporal_order(train_times: Sequence[datetime], eval_times: Sequence[datetime]) -> dict[str, Any]:
    if not train_times or not eval_times:
        return _c("temporal_order", "not_checked", "no timestamps supplied for one of the sets")
    last_train, first_eval = max(train_times), min(eval_times)
    if first_eval <= last_train:
        late = sum(1 for t in eval_times if t <= last_train)
        return _c("temporal_order", "fail", f"{late} evaluation record(s) are not later than the newest training record ({last_train.isoformat()}); a random or overlapping split lets the model see the future", overlap=late)  # fmt: skip
    return _c("temporal_order", "pass", "every evaluation record is later than every training record")


def group_overlap(
    train_groups: Sequence[Hashable], eval_groups: Sequence[Hashable], label: str = "entity"
) -> dict[str, Any]:
    if not train_groups or not eval_groups:
        return _c(f"{label}_grouping", "not_checked", "no group identifiers supplied")
    shared = set(train_groups) & set(eval_groups)
    if shared:
        return _c(f"{label}_grouping", "fail", f"{len(shared)} {label} group(s) appear in both training and evaluation, so related rows leak across the split", overlap=len(shared))  # fmt: skip
    return _c(f"{label}_grouping", "pass", f"no {label} group appears in both training and evaluation")


def duplicate_overlap(train_keys: Sequence[Hashable], eval_keys: Sequence[Hashable]) -> dict[str, Any]:
    if not train_keys or not eval_keys:
        return _c("duplicate_rows", "not_checked", "no row keys supplied")
    shared = set(train_keys) & set(eval_keys)
    return _c("duplicate_rows", "fail" if shared else "pass", f"{len(shared)} identical record(s) appear in both sets" if shared else "no identical record appears in both sets", overlap=len(shared))  # fmt: skip


def target_in_features(feature_names: Sequence[str], target: str, columns: dict[str, Sequence[float]] | None = None, y: Sequence[float] | None = None, corr_limit: float = 0.999) -> dict[str, Any]:  # fmt: skip
    t = target.lower().replace("-", "_")
    named = [f for f in feature_names if f.lower().replace("-", "_") == t or f.lower().replace("-", "_").startswith(f"{t}_actual") or f.lower() in ("label", "target", "outcome")]  # fmt: skip
    near: list[str] = []
    if columns and y is not None:
        for name, col in columns.items():
            r = _pearson(col, y)
            if r is not None and abs(r) >= corr_limit:
                near.append(name)
    bad = sorted(set(named) | set(near))
    if bad:
        return _c(
            "target_in_features", "fail", f"feature(s) {', '.join(bad)} equal or reproduce the target", features=bad
        )
    return _c(
        "target_in_features",
        "pass",
        "no feature carries the target by name" + ("" if columns else " (correlation not checked: no values supplied)"),
    )


def _pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n < 3 or n != len(y):
        return None
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx < 1e-12 or sy < 1e-12:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / (sx * sy)


def fit_boundary(declared: dict[str, str] | None) -> dict[str, Any]:
    """``declared`` maps each fitted artefact (scaler, imputer, encoder, threshold, calibration, selection) to the split it was fitted on."""
    if not declared:
        return _c(
            "fit_boundaries",
            "not_checked",
            "the run does not declare what each preprocessing, threshold or calibration step was fitted on",
        )
    bad = {
        k: v for k, v in declared.items() if str(v).lower() in ("test", "holdout", "final", "all", "full", "evaluation")
    }
    if bad:
        return _c("fit_boundaries", "fail", "fitted on data that includes the final evaluation set: " + ", ".join(f"{k} ← {v}" for k, v in sorted(bad.items())), steps=sorted(bad))  # fmt: skip
    return _c("fit_boundaries", "pass", "every declared fitted step used training or validation data only")


def window_order(train_end: datetime | None, val_end: datetime | None, test_start: datetime | None) -> dict[str, Any]:
    if None in (train_end, test_start):
        return _c("window_order", "not_checked", "window bounds are not stored")
    if test_start <= train_end:  # type: ignore[operator]
        return _c("window_order", "fail", "the test window starts before the training window ends")
    if val_end is not None and not (train_end <= val_end <= test_start):  # type: ignore[operator]
        return _c("window_order", "fail", "validation window is not between training and test")
    return _c("window_order", "pass", "training < validation < test in time")


def overall(checks: list[dict[str, Any]]) -> str:
    if any(c["status"] == "fail" for c in checks):
        return "invalid"
    if any(c["status"] == "not_checked" for c in checks):
        return "valid_with_unchecked"
    return "valid"
