"""Closed model registry. Each model declares eligibility, minimum history, supported frequencies, interval support and
a version; fitting is deterministic (no random numbers) and failures are explicit (``ModelFailure``), never silent."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.forecast import features as feat
from app.forecast.stats import mean, ridge_fit

MODEL_VERSION = "1"


class ModelFailure(Exception):
    """A model could not be fitted or produced a non-finite/explosive forecast."""


@dataclass
class ModelOutput:
    values: list[float]
    params: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    converged: bool = True


Fn = Callable[[list[float], int, int | None, list[date], str], ModelOutput]


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    family: str
    rank: int  # complexity rank used for tie-breaking (lower = simpler)
    fn: Fn
    min_history: Callable[[int | None], int]
    needs_season: bool = False
    intermittent_only: bool = False
    frequencies: tuple[str, ...] = ("daily", "weekly")
    interval_support: str = "empirical_rolling_origin"
    schema: dict[str, Any] = field(default_factory=dict)
    version: str = MODEL_VERSION

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "family": self.family, "complexity_rank": self.rank,
            "version": self.version, "frequencies": list(self.frequencies), "needs_seasonal_period": self.needs_season,
            "intermittent_only": self.intermittent_only, "interval_support": self.interval_support,
            "min_history_example": {"season_7": self.min_history(7), "no_season": self.min_history(None)},
            "parameters": self.schema,
        }  # fmt: skip


def _finite(vals: list[float], key: str) -> list[float]:
    if any((not math.isfinite(v)) for v in vals):
        raise ModelFailure(f"{key}: non-finite forecast")
    return vals


def _naive(y, h, m, d, f):
    return ModelOutput([y[-1]] * h)


def _snaive(y, h, m, d, f):
    if not m or len(y) < m:
        raise ModelFailure("seasonal_naive: needs a seasonal period and at least one full cycle")
    return ModelOutput([y[len(y) - m + (i % m)] for i in range(h)], {"period": m})


def _mean(y, h, m, d, f):
    return ModelOutput([sum(y) / len(y)] * h)


def _ma(y, h, m, d, f):
    w = min(m or 4, len(y))
    return ModelOutput([sum(y[-w:]) / w] * h, {"window": w})


def _sse_ses(y, a):
    lvl, sse = y[0], 0.0
    for v in y[1:]:
        sse += (v - lvl) ** 2
        lvl += a * (v - lvl)
    return sse, lvl


def _ses(y, h, m, d, f):
    best = min((_sse_ses(y, a)[0], a) for a in (0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9))
    lvl = _sse_ses(y, best[1])[1]
    return ModelOutput([lvl] * h, {"alpha": best[1]})


def _holt(y, h, m, d, f):
    n = len(y)
    k = min(4, n - 1)
    b0 = (y[k] - y[0]) / k
    best = None
    for a in (0.1, 0.3, 0.5, 0.8):
        for bt in (0.05, 0.1, 0.2, 0.4):
            for phi in (0.8, 0.9, 0.98):
                lvl, b, sse = y[0], b0, 0.0
                for v in y[1:]:
                    pred = lvl + phi * b
                    sse += (v - pred) ** 2
                    new = pred + a * (v - pred)
                    b = phi * b + a * bt * (v - pred)
                    lvl = new
                if best is None or sse < best[0]:
                    best = (sse, a, bt, phi, lvl, b)
    _, a, bt, phi, lvl, b = best  # type: ignore[misc]
    out, cum = [], 0.0
    for i in range(1, h + 1):
        cum += phi**i
        out.append(lvl + cum * b)
    return ModelOutput(_finite(out, "holt_damped"), {"alpha": a, "beta": bt, "phi": phi})


def _hw(y, h, m, d, f):
    n = len(y)
    if not m or n < 2 * m:
        raise ModelFailure("holt_winters: needs a seasonal period and two full cycles")
    l0 = sum(y[:m]) / m
    b0 = (sum(y[m : 2 * m]) / m - l0) / m
    s0 = [y[i] - l0 for i in range(m)]
    best = None
    for a in (0.1, 0.3, 0.5):
        for bt in (0.01, 0.05, 0.15):
            for g in (0.1, 0.3, 0.5):
                lvl, b, s, sse = l0, b0, s0[:], 0.0
                for t in range(m, n):
                    pred = lvl + b + s[t % m]
                    e = y[t] - pred
                    sse += e * e
                    nl = lvl + b + a * e
                    b = b + a * bt * e
                    s[t % m] = s[t % m] + g * (1 - a) * e
                    lvl = nl
                if best is None or sse < best[0]:
                    best = (sse, a, bt, g, lvl, b, s)
    _, a, bt, g, lvl, b, s = best  # type: ignore[misc]
    out = [lvl + (i + 1) * b + s[(n + i) % m] for i in range(h)]
    return ModelOutput(_finite(out, "holt_winters"), {"alpha": a, "beta": bt, "gamma": g, "period": m})


def _arima(y, h, m, d, f):
    if len(y) < 20:
        raise ModelFailure("arima_like: needs at least 20 periods")
    best = None
    for dd in (0, 1):
        z = y if dd == 0 else [y[i + 1] - y[i] for i in range(len(y) - 1)]
        for p in (1, 2, 3):
            if len(z) - p < 3 * (p + 1):
                continue
            rows = [[1.0] + [z[t - j] for j in range(1, p + 1)] for t in range(p, len(z))]
            tgt = z[p:]
            sc = max(1e-9, max(abs(v) for v in z))
            beta = ridge_fit([[r[0]] + [v / sc for v in r[1:]] for r in rows], [v / sc for v in tgt], 0.0)
            if beta is None:
                continue
            c, phis = beta[0] * sc, beta[1:]
            sse = sum(
                (tg - c - sum(ph * r[1 + j] for j, ph in enumerate(phis))) ** 2 for r, tg in zip(rows, tgt, strict=True)
            )
            ne, k = len(tgt), p + 1
            if sse <= 1e-12 or ne - k - 1 <= 0:
                aicc = -math.inf if sse <= 1e-12 else math.inf
            else:
                aicc = ne * math.log(sse / ne) + 2 * k + 2 * k * (k + 1) / (ne - k - 1)
            # stationarity: impulse response of the AR part must not grow
            psi, hist = [1.0], [1.0] + [0.0] * (p - 1)
            for _ in range(120):
                nxt = sum(ph * hist[j] for j, ph in enumerate(phis))
                hist = [nxt, *hist[:-1]]
                psi.append(nxt)
            if max(abs(v) for v in psi) > 20 or abs(psi[-1]) > 1e-2 * max(1.0, abs(psi[0])) * 50:
                continue
            if best is None or aicc < best[0]:
                best = (aicc, dd, p, c, phis, z)
    if best is None:
        raise ModelFailure("arima_like: no stationary, numerically stable specification was found")
    _, dd, p, c, phis, z = best
    hist = z[:]
    fz = []
    for _ in range(h):
        v = c + sum(ph * hist[-1 - j] for j, ph in enumerate(phis))
        hist.append(v)
        fz.append(v)
    out = fz if dd == 0 else [y[-1] + sum(fz[: i + 1]) for i in range(h)]
    cap = 20 * max(abs(v) for v in y) + 1
    if any(abs(v) > cap for v in out):
        raise ModelFailure("arima_like: forecast diverged")
    return ModelOutput(_finite(out, "arima_like"), {"p": p, "d": dd, "q": 0, "aicc": round(best[0], 4) if math.isfinite(best[0]) else None, "estimation": "ordinary least squares (no iterative optimiser)"})  # fmt: skip


def _croston(y, h, m, d, f):
    if not any(v > 0 for v in y):
        return ModelOutput([0.0] * h, {"note": "no non-zero demand in the history"}, ["All history is zero."])
    best = None
    for a in (0.1, 0.2, 0.3):
        z = p = None
        q = 1
        err, cnt = 0.0, 0
        for v in y:
            if z is not None:
                err += abs(v - z / p)
                cnt += 1
            if v > 0:
                if z is None:
                    z, p = v, float(q)
                else:
                    z += a * (v - z)
                    p += a * (q - p)
                q = 1
            else:
                q += 1
        if best is None or err < best[0]:
            best = (err, a, z, p)
    _, a, z, p = best  # type: ignore[misc]
    rate = (1 - a / 2) * z / p
    return ModelOutput([rate] * h, {"alpha": a, "variant": "SBA", "size": z, "interval": p}, [])


def _ridge(y, h, m, d, f):
    scale = (mean(y) or 0.0) or 1.0
    if scale <= 0:
        raise ModelFailure("lag_ridge: non-positive scale")
    xs, ys = feat.matrix(y, d, m, f, scale)
    if len(xs) < 2 * len(xs[0]) if xs else True:
        raise ModelFailure("lag_ridge: too few training rows for the number of features")
    beta = ridge_fit(xs, ys, 1e-3)
    if beta is None:
        raise ModelFailure("lag_ridge: singular normal equations")
    hist = y[:]
    dts = feat.calendar_dates(d[-1], h, f)
    out = []
    for i in range(h):
        r = feat.row(hist, dts[i], m, f, scale)
        v = scale * sum(b * x for b, x in zip(beta, r, strict=True))
        if not math.isfinite(v) or abs(v) > 20 * max(abs(t) for t in y) + 1:
            raise ModelFailure("lag_ridge: recursive forecast diverged")
        hist.append(v)
        out.append(v)
    return ModelOutput(out, {"features": feat.names(m, f), "l2": 1e-3, "recursive": True, "train_rows": len(xs)})


def _min(lo: int, per_season: float = 0):
    return lambda m: max(lo, int(per_season * m) if m else lo)


REGISTRY: dict[str, ModelSpec] = {
    s.key: s
    for s in (
        ModelSpec("naive", "Naive (last value)", "baseline", 0, _naive, _min(1)),
        ModelSpec("seasonal_naive", "Seasonal naive", "baseline", 1, _snaive, lambda m: m or 10**9, needs_season=True),
        ModelSpec("historical_mean", "Historical mean", "baseline", 2, _mean, _min(3)),
        ModelSpec(
            "moving_average",
            "Moving average",
            "smoothing",
            3,
            _ma,
            _min(3),
            schema={"window": "seasonal period, else 4"},
        ),
        ModelSpec(
            "ses",
            "Simple exponential smoothing",
            "smoothing",
            4,
            _ses,
            _min(8),
            schema={"alpha_grid": [0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9]},
        ),
        ModelSpec(
            "holt_damped",
            "Holt damped trend",
            "smoothing",
            5,
            _holt,
            _min(12),
            schema={
                "alpha_grid": [0.1, 0.3, 0.5, 0.8],
                "beta_grid": [0.05, 0.1, 0.2, 0.4],
                "phi_grid": [0.8, 0.9, 0.98],
            },
        ),  # fmt: skip
        ModelSpec(
            "holt_winters",
            "Additive Holt-Winters",
            "smoothing",
            7,
            _hw,
            lambda m: 2 * m if m else 10**9,
            needs_season=True,
            schema={"alpha_grid": [0.1, 0.3, 0.5], "beta_grid": [0.01, 0.05, 0.15], "gamma_grid": [0.1, 0.3, 0.5]},
        ),  # fmt: skip
        ModelSpec(
            "arima_like",
            "ARIMA(p,d,0) by AICc",
            "statistical",
            8,
            _arima,
            _min(20),
            schema={"p": [1, 2, 3], "d": [0, 1], "q": 0, "selection": "AICc"},
        ),  # fmt: skip
        ModelSpec(
            "croston_sba",
            "Croston (Syntetos-Boylan)",
            "intermittent",
            6,
            _croston,
            _min(10),
            intermittent_only=True,
            schema={"alpha_grid": [0.1, 0.2, 0.3]},
        ),  # fmt: skip
        ModelSpec(
            "lag_ridge",
            "Lag/calendar ridge regression",
            "ml",
            9,
            _ridge,
            lambda m: max(40, 4 * (m or 7)),
            schema={"l2": 1e-3, "features": "see feature lineage"},
        ),  # fmt: skip
    )
}


def eligibility(
    spec: ModelSpec, *, n: int, season: int | None, freq: str, zero_prop: float, nonzero: int
) -> tuple[bool, str | None]:
    if freq not in spec.frequencies:
        return False, f"frequency {freq} unsupported"
    if spec.needs_season and not season:
        return False, "no seasonal period configured"
    need = spec.min_history(season)
    if n < need:
        return False, f"needs {need} periods, have {n}"
    if spec.intermittent_only and not (zero_prop >= 0.25 and nonzero >= 3):
        return False, "series is not intermittent (zero proportion < 0.25 or fewer than 3 non-zero periods)"
    return True, None


def run_model(key: str, y: list[float], h: int, season: int | None, dates: list[date], freq: str) -> ModelOutput:
    spec = REGISTRY[key]
    if len(y) < spec.min_history(season):
        raise ModelFailure(f"{key}: insufficient history ({len(y)} < {spec.min_history(season)})")
    try:
        out = spec.fn(y, h, season, dates, freq)
    except ModelFailure:
        raise
    except (ZeroDivisionError, OverflowError, ValueError, IndexError) as e:
        raise ModelFailure(f"{key}: {type(e).__name__}") from e
    if len(out.values) != h or any(not math.isfinite(v) for v in out.values):
        raise ModelFailure(f"{key}: invalid forecast")
    return out
