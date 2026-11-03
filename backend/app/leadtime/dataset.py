"""Training examples and the chronological, group-aware, purged split.

Each eligible delivery is one example with a reference time (the prediction point), a label and the time the label became
known. Splits are cut on reference time; all lines of one order stay in the same split; training (and validation)
examples whose outcome was not yet known when the next split begins are PURGED so no label from the future of a later
split's origin can be learned from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.leadtime.analytics import Obs
from app.leadtime.config import LeadTimeConfig
from app.leadtime.features import reference_time
from app.leadtime.history import HF, Outcome, Sweep
from app.leadtime.semantics import Delivery


@dataclass
class Ex:
    d: Delivery
    ref_at: datetime
    known_at: datetime
    y: float  # lead time in the configured unit, or 1.0/0.0 for late
    hf: HF | None = None


def outcomes_of(complete: list[Obs]) -> list[Outcome]:
    return [Outcome(o.ref_at, o.d.supplier_id, o.d.lane, o.value, o.late) for o in complete if o.ref_at is not None]


def build_examples(part: dict[str, Any], cfg: LeadTimeConfig) -> tuple[list[Ex], dict[str, int]]:
    """Eligible examples for the configured target and prediction point, plus exclusion counts."""
    ex: list[Ex] = []
    skipped: dict[str, int] = {}

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    for o in part["complete"]:
        d = o.d
        ref = reference_time(d, cfg.prediction_point)
        if cfg.target == "LEAD_TIME":
            if o.value is None:
                skip("NO_LEAD_TIME_FOR_DEFINITION")
                continue
            y, known = o.value, o.ref_at
        else:
            if o.late is None:
                skip("NO_COMMITMENT_AMBIGUOUS_LABEL")
                continue
            y, known = (1.0 if o.late else 0.0), d.arrived_at
        if ref is None:
            skip("NO_REFERENCE_TIME_FOR_PREDICTION_POINT")
            continue
        if known is None or known < ref:
            skip("OUTCOME_BEFORE_PREDICTION_POINT")
            continue
        ex.append(Ex(d, ref, known, y))
    return ex, skipped


def attach_history(examples: list[Ex], outcomes: list[Outcome]) -> None:
    sw = Sweep(outcomes)
    for e in sorted(examples, key=lambda x: (x.ref_at, x.d.key)):
        e.hf = sw.at(e.ref_at, e.d.supplier_id, e.d.lane)


def split(examples: list[Ex], cfg: LeadTimeConfig) -> dict[str, Any]:
    groups: dict[str, list[Ex]] = {}
    for e in examples:
        groups.setdefault(e.d.ref, []).append(e)
    order = sorted(groups.values(), key=lambda g: (min(x.ref_at for x in g), g[0].d.ref))
    n = len(examples)
    t1, t2 = int(n * cfg.train_fraction), int(n * (cfg.train_fraction + cfg.validation_fraction))
    train: list[Ex] = []
    val: list[Ex] = []
    test: list[Ex] = []
    seen = 0
    for g in order:
        (train if seen < t1 else val if seen < t2 else test).extend(g)
        seen += len(g)
    out: dict[str, Any] = {"train": train, "val": val, "test": test, "purged_train": 0, "purged_val": 0}
    if val:
        cut = min(x.ref_at for x in val)
        keep = [x for x in train if x.known_at <= cut]
        out["purged_train"], out["train"] = len(train) - len(keep), keep
    if test:
        cut = min(x.ref_at for x in test)
        keep_v = [x for x in out["val"] if x.known_at <= cut]
        out["purged_val"], out["val"] = len(out["val"]) - len(keep_v), keep_v
        keep_t = [x for x in out["train"] if x.known_at <= cut]
        out["purged_train"] += len(out["train"]) - len(keep_t)
        out["train"] = keep_t
    return out


def split_report(sp: dict[str, Any]) -> dict[str, Any]:
    def span(xs: list[Ex]) -> dict[str, Any]:
        return {"n": len(xs), "first_reference": min(x.ref_at for x in xs).isoformat() if xs else None, "last_reference": max(x.ref_at for x in xs).isoformat() if xs else None}  # fmt: skip

    return {
        "scheme": "chronological train / validation / test by prediction-point time; whole orders stay together; examples whose outcome was unknown at the next split's start are purged",  # fmt: skip
        "train": span(sp["train"]), "validation": span(sp["val"]), "test": span(sp["test"]),
        "purged_from_train": sp["purged_train"], "purged_from_validation": sp["purged_val"],
    }  # fmt: skip
