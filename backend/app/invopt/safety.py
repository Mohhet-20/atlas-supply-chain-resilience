"""Safety stock, reorder point and service-level mathematics (pure, no database).

Notation: ``d`` mean demand per day, ``sd`` demand standard deviation per day, ``L`` mean lead time (days), ``sL`` lead-time standard
deviation (days), ``R`` review period (days; 0 for continuous review). The protection interval is ``P = L + R``.

Demand over the protection interval has mean ``d P`` and, with independent days and a lead time independent of demand,
variance ``P sd^2 + d^2 sL^2``. Cycle service level alpha: ``SS = z_alpha sigma``. Fill rate beta with order quantity ``Q``:
``SS = k sigma`` where the normal loss function satisfies ``G(k) = (1 - beta) Q / sigma``.

A result that cannot be defended is returned with ``value = None`` and a reason; it is never replaced with a guess.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from statistics import NormalDist
from typing import Any

ND = NormalDist()
EPS = 1e-9


def z(p: float) -> float:
    return ND.inv_cdf(p)


def loss(k: float) -> float:
    """Standard normal first-order loss G(k) = E[(Z - k)+] = phi(k) - k (1 - Phi(k))."""
    return ND.pdf(k) - k * ND.cdf(-k)


def inverse_loss(g: float) -> float:
    """The k with G(k) = g (G is strictly decreasing). Clamped to [-8, 8]."""
    lo, hi = -8.0, 8.0
    if g >= loss(lo):
        return lo
    if g <= loss(hi):
        return hi
    for _ in range(100):
        mid = (lo + hi) / 2
        if loss(mid) > g:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def r6(x: float | None) -> float | None:
    return None if x is None else round(float(x), 6)


# ------------------------------------------------------------------------------------------------ demand statistics


def demand_stats(periods: Sequence[tuple[int, float]]) -> dict[str, Any]:
    """Per-day mean and standard deviation from (days, quantity) periods.

    sd^2 = sum((D_p - n_p r)^2 / n_p) / (K - 1), r = sum D / sum n: for independent days, D_p has variance n_p sd^2, so each term
    estimates sd^2 whatever the period length. Needs K >= 3 periods. ``intermittent`` is judged on the periods supplied (a weekly
    bucket cannot reveal zero days inside it - recorded in ``resolution_days``).
    """
    k = len(periods)
    out: dict[str, Any] = {"periods": k, "covered_days": 0, "mean_per_day": None, "sd_per_day": None, "zero_share": None, "intermittent": False, "resolution_days": None, "cv": None}  # fmt: skip
    if k == 0:
        return out
    days = sum(n for n, _ in periods)
    total = sum(q for _, q in periods)
    out["covered_days"] = days
    out["resolution_days"] = max(n for n, _ in periods)
    r = total / days
    out["mean_per_day"] = r6(r)
    zero = sum(1 for _, q in periods if q == 0)
    out["zero_share"] = r6(zero / k)
    out["intermittent"] = zero / k >= 0.5
    if k >= 3:
        s2 = sum((q - n * r) ** 2 / n for n, q in periods) / (k - 1)
        out["sd_per_day"] = r6(math.sqrt(s2))
        out["cv"] = r6(math.sqrt(s2) / r) if r > EPS else None
    return out


# ------------------------------------------------------------------------------------------------ analytic methods


def protection_moments(d: float, sd: float, lead: float, lead_sd: float, review: float) -> tuple[float, float, float]:
    """(mean, sigma, P) of demand over the protection interval."""
    p = lead + review
    var = p * sd * sd + (d * lead_sd) ** 2
    return d * p, math.sqrt(max(0.0, var)), p


def implied_service(ss: float, sigma: float, q: float | None) -> dict[str, Any]:
    """Cycle service level and fill rate implied by a safety stock under a normal protection-interval demand."""
    if sigma <= EPS:
        return {"cycle_service_level": 1.0 if ss >= 0 else 0.0, "fill_rate": 1.0 if ss >= 0 else None, "k": None}
    k = ss / sigma
    fr = None if not q else max(0.0, min(1.0, 1.0 - sigma * loss(k) / q))
    return {"cycle_service_level": r6(ND.cdf(k)), "fill_rate": r6(fr), "k": r6(k)}


def normal_safety_stock(
    *, d: float, sd: float, lead: float, lead_sd: float | None, review: float, service_type: str, target: float, q: float | None,
) -> dict[str, Any]:  # fmt: skip
    if not (0.5 <= target < 1):
        return _none("The service target must be at least 0.5 and below 1.")
    sl = lead_sd or 0.0
    mean, sigma, p = protection_moments(d, sd, lead, sl, review)
    res: dict[str, Any] = {"value": None, "method": "NORMAL_LT" if lead_sd else "NORMAL", "service_type": service_type, "target": target, "protection_days": r6(p), "mean_protection_demand": r6(mean), "sigma_protection": r6(sigma), "k": None, "reason": None, "assumptions": [], "warnings": []}  # fmt: skip
    res["formula"] = "z(alpha) x sigma" if service_type == "CYCLE" else "k x sigma with G(k) = (1 - beta) Q / sigma"
    res["assumptions"] += [
        "demand per day independent and stationary over the window",
        "lead time independent of demand",
    ]
    if not lead_sd:
        res["assumptions"].append("lead-time variability is not known and is excluded; safety stock may be understated")
    if service_type == "CYCLE":
        k = z(target)
    else:
        if not q or q <= 0:
            res["reason"] = (
                "A fill-rate target needs an order quantity Q (the expected shortage per cycle is divided by Q)."
            )
            return res
        if sigma <= EPS:
            k = 0.0
        else:
            k = inverse_loss((1 - target) * q / sigma)
    res["k"] = r6(k)
    ss = k * sigma
    if ss < 0:
        res["warnings"].append(
            "The required k is negative (the order quantity alone gives the target fill rate); safety stock is set to 0."
        )
        ss = 0.0
    res["value"] = r6(ss)
    res["implied"] = implied_service(ss, sigma, q)
    return res


def _none(reason: str) -> dict[str, Any]:
    return {"value": None, "reason": reason, "method": None, "assumptions": [], "warnings": []}


# --------------------------------------------------------------------------------------------------------- bootstrap


def bootstrap_protection_demand(
    daily: Sequence[float], lead_samples: Sequence[float], review: int, n_sims: int, rng: random.Random
) -> list[float]:
    """Sorted sums of demand over simulated protection intervals: lead time drawn from observed values (rounded up to whole
    days, minimum 1) plus the review period; each day's demand drawn independently from the observed daily demands."""
    out = []
    for _ in range(n_sims):
        lt = lead_samples[rng.randrange(len(lead_samples))]
        p = max(1, math.ceil(lt - 1e-9)) + review
        out.append(sum(rng.choices(daily, k=p)))
    out.sort()
    return out


def _quantile(sorted_vals: list[float], p: float) -> float:
    n = len(sorted_vals)
    return sorted_vals[min(n - 1, max(0, math.ceil(p * n) - 1))]


def bootstrap_safety_stock(
    *, daily: Sequence[float], lead_samples: Sequence[float], review: int, service_type: str, target: float, q: float | None,
    n_sims: int = 4000, seed: int = 0,
) -> dict[str, Any]:  # fmt: skip
    rng = random.Random(seed)
    sums = bootstrap_protection_demand(daily, lead_samples, review, n_sims, rng)
    mean = sum(sums) / len(sums)
    res: dict[str, Any] = {"value": None, "method": "BOOTSTRAP", "service_type": service_type, "target": target, "mean_protection_demand": r6(mean), "simulations": n_sims, "seed": seed, "reason": None, "assumptions": ["observed days resampled independently (no seasonality or autocorrelation)", "lead time drawn from observed values"], "warnings": []}  # fmt: skip
    if service_type == "CYCLE":
        reorder = _quantile(sums, target)
        res["formula"] = "empirical alpha-quantile of protection-interval demand minus its mean"
    else:
        if not q or q <= 0:
            res["reason"] = "A fill-rate target needs an order quantity Q."
            return res
        allowed = (1 - target) * q
        lo, hi = 0.0, sums[-1]

        def short(s: float) -> float:
            return sum(max(0.0, x - s) for x in sums) / len(sums)

        if short(lo) <= allowed:
            reorder = 0.0
        else:
            for _ in range(60):
                mid = (lo + hi) / 2
                if short(mid) > allowed:
                    lo = mid
                else:
                    hi = mid
            reorder = hi
        res["formula"] = "smallest stock s with simulated expected shortage per cycle <= (1 - beta) Q"
    ss = max(0.0, reorder - mean)
    if reorder - mean < 0:
        res["warnings"].append("The empirical quantile is below the mean protection demand; safety stock is set to 0.")
    res["value"] = r6(ss)
    res["reorder_level"] = r6(reorder)
    res["empirical_cycle_service_level"] = r6(sum(1 for x in sums if x <= reorder + 1e-12) / len(sums))
    if len(daily) < 60:
        res["warnings"].append(
            f"Only {len(daily)} observed days: tail quantiles are poorly determined; treat the value as indicative."
        )
    return res


# ------------------------------------------------------------------------------------------------------ method choice


def choose_method(
    *, requested: str, demand: dict[str, Any], lead_sd_known: bool, daily_len: int, min_bootstrap_days: int
) -> tuple[str | None, str | None]:
    """(method, reason-if-none). Never silently picks a method whose assumptions the data contradicts."""
    if demand.get("mean_per_day") is None:
        return None, "Demand per day is not available, so no safety stock can be calculated."
    intermittent = bool(demand.get("intermittent"))
    can_boot = daily_len >= min_bootstrap_days and demand.get("resolution_days") == 1
    sd = demand.get("sd_per_day")
    if requested == "BOOTSTRAP":
        return (
            ("BOOTSTRAP", None)
            if can_boot
            else (None, f"BOOTSTRAP needs at least {min_bootstrap_days} days of daily demand observations.")
        )
    if requested in ("NORMAL", "NORMAL_LT"):
        if intermittent:
            return (
                None,
                "The normal methods are not valid for intermittent demand (half or more of the periods are zero).",
            )
        if sd is None:
            return None, "Demand variability needs at least 3 observed periods."
        if requested == "NORMAL_LT" and not lead_sd_known:
            return None, "NORMAL_LT needs a measured or supplied lead-time standard deviation."
        return requested, None
    # AUTO
    if intermittent:
        return ("BOOTSTRAP", None) if can_boot else (None, "Demand is intermittent and there is not enough daily history for the empirical method; safety stock cannot be reliably calculated.")  # fmt: skip
    if sd is None:
        return (
            ("BOOTSTRAP", None)
            if can_boot
            else (None, "Demand variability cannot be estimated (fewer than 3 periods).")
        )
    return ("NORMAL_LT" if lead_sd_known else "NORMAL"), None
