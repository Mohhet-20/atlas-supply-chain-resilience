"""Leakage-safe feature construction. Every feature for target index ``t`` is computed from ``y[:t]`` only
(lags and rolling statistics are shifted by construction); calendar features depend only on the target's date."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.forecast.stats import mean

FEATURE_VERSION = "fc-feat-1"


def names(season: int | None, freq: str) -> list[str]:
    n = ["intercept", "lag_1", "lag_2", "roll_mean"]
    if season:
        n.append(f"lag_{season}")
    if freq == "daily":
        n += [f"dow_{k}" for k in range(1, 7)]
    return n


def min_rows(season: int | None) -> int:
    return max(3, (season or 0) + 1, 8)


def row(hist: list[float], target_date: date, season: int | None, freq: str, scale: float) -> list[float]:
    """Feature row for the period *after* ``hist``. ``hist`` must contain only strictly earlier observations."""
    w = season or 4
    r = [1.0, hist[-1] / scale, hist[-2] / scale, (mean(hist[-w:]) or 0.0) / scale]
    if season:
        r.append(hist[-season] / scale)
    if freq == "daily":
        dow = target_date.weekday()
        r += [1.0 if dow == k else 0.0 for k in range(1, 7)]
    return r


def matrix(
    y: list[float], dates: list[date], season: int | None, freq: str, scale: float
) -> tuple[list[list[float]], list[float]]:
    start = min_rows(season)
    xs, ys = [], []
    for t in range(start, len(y)):
        xs.append(row(y[:t], dates[t], season, freq, scale))
        ys.append(y[t] / scale)
    return xs, ys


def lineage(season: int | None, freq: str) -> dict[str, Any]:
    return {
        "feature_version": FEATURE_VERSION, "features": names(season, freq),
        "definitions": {
            "lag_k": "demand k periods before the target period", "roll_mean": "mean of the previous `season or 4` periods (shifted: excludes the target)",  # fmt: skip
            "dow_k": "calendar indicator from the target date (known in advance)",
        },
        "leakage_rule": "features for index t use y[:t] only; the target and later values are never read",
        "exogenous": "none — no exogenous demand drivers exist in the platform data",
    }  # fmt: skip


def calendar_dates(last: date, h: int, freq: str) -> list[date]:
    step = 1 if freq == "daily" else 7
    return [last + timedelta(days=step * (i + 1)) for i in range(h)]
