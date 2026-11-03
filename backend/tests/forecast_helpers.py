"""SYNTHETIC forecasting fixtures. Every number here is invented; none represents real demand."""

from __future__ import annotations

import random
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.forecast import runs
from app.forecast.config import ForecastConfig
from app.models.forecast import ForecastRun, ForecastValue
from tests.inventory_helpers import (  # noqa: F401
    TABLE,
    TODAY,
    TOK,
    code,
    d,
    h,
    iso,
    load_demand,
    load_inventory,
    load_receipts,
    world,
)

FC = "/api/v1/forecast"


def series_rows(
    w, p: str, f: str, values: list[float | None], unit: str = "EA", end_days_ago: int = 0
) -> list[list[Any]]:
    """Daily CONSUMPTION rows; ``values[-1]`` is the day ending ``end_days_ago`` days ago. ``None`` = no row (missing)."""
    n = len(values)
    rows = []
    for i, v in enumerate(values):
        if v is None:
            continue
        ago = end_days_ago + (n - 1 - i)
        rows.append([code(w, p), code(w, f), "CONSUMPTION", d(ago + 1), d(ago), v, unit])
    return rows


def stable(n=120, seed=1, level=50.0):
    r = random.Random(seed)
    return [level + r.gauss(0, 2) for _ in range(n)]


def trending(n=120, seed=2):
    r = random.Random(seed)
    return [20 + 0.5 * i + r.gauss(0, 2) for i in range(n)]


def seasonal(n=140, seed=3):
    r = random.Random(seed)
    shape = [0.6, 0.7, 0.8, 0.9, 1.0, 1.9, 2.1]
    return [100 * shape[i % 7] + r.gauss(0, 3) for i in range(n)]


def intermittent(n=150, seed=4):
    r = random.Random(seed)
    return [float(r.choice([3, 5, 8])) if r.random() < 0.3 else 0.0 for _ in range(n)]


def with_gap(vals, a=60, b=62):
    return [None if a <= i <= b else v for i, v in enumerate(vals)]


def synthetic_world(client) -> dict[str, Any]:
    """P1@F1 stable (+inventory +receipt); P1@F2 trending; P2@F1 seasonal; P2@F2 intermittent; P3@F1 missing-period;
    P3@F2 insufficient history."""
    w = world(client)
    rows = (
        series_rows(w, "p1", "f1", stable())
        + series_rows(w, "p1", "f2", trending())
        + series_rows(w, "p2", "f1", seasonal())
        + series_rows(w, "p2", "f2", intermittent())
        + series_rows(w, "p3", "f1", with_gap(stable(100, 5, 30.0)), unit="KG")
        + series_rows(w, "p3", "f2", stable(8, 6, 10.0), unit="KG")
    )
    load_demand(client, rows)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(0.1), "EA", 600, 0, 0, 0, 0, "", ""]])
    load_receipts(client, [[code(w, "p1"), code(w, "f1"), "PO-F1", "CONFIRMED", 400, "EA", d(-10), code(w, "s1")]])
    return w


def pid(client, w, key: str) -> int:
    return int(w[key]["id"])


def forecast(client, w, p: str, f: str, conf: ForecastConfig | None = None, **kw) -> ForecastRun:
    return runs.forecast_now(client.app.state.session_factory, pid(client, w, p), pid(client, w, f), "tester", kw.pop("limits", runs.RunLimits()), conf or ForecastConfig(horizon=14, n_folds=5), **kw)  # fmt: skip


def values_of(client, run_id: int) -> list[ForecastValue]:
    with Session(client.app.state.engine) as s:
        rows = list(s.scalars(select(ForecastValue).where(ForecastValue.run_id == run_id).order_by(ForecastValue.step)))
        for r in rows:
            s.expunge(r)
        return rows
