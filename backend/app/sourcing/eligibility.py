"""Hard eligibility: which candidates may be allocated at all. Separate from ranking and optimisation.

Every rule records PASS / FAIL / UNKNOWN evidence. The status of a candidate is the most severe non-pass outcome, and only
ELIGIBLE candidates are ever allocated:

  ELIGIBLE                      every required rule passed on stated data
  ELIGIBLE_AFTER_QUALIFICATION  would pass, but the supplier or offer is still pending qualification
  MISSING_INFORMATION           potentially eligible, but a required fact is absent or in another unit/currency - NOT assumed
  UNAVAILABLE                   temporarily not usable (on hold, suspended, not yet in force, no capacity)
  EXCLUDED                      removed by a request filter or threshold (lead time, defect rate, risk, lists, countries)
  DISQUALIFIED                  explicitly or structurally not allowed (rejected approval, expired contract, inactive, merged)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.sourcing.config import STATUS_ORDER, SourcingRequest
from app.sourcing.model import Candidate

SEVERITY = {s: i for i, s in enumerate(STATUS_ORDER)}


@dataclass(frozen=True)
class Context:
    req: SourcingRequest
    now: datetime
    product_ok: bool = True
    product_note: str = ""
    destination_ok: bool = True
    destination_note: str = ""
    reliability_min_sample: int = 20


def _ev(rule: str, outcome: str, detail: str) -> dict[str, str]:
    return {"rule": rule, "outcome": outcome, "detail": detail}


def assess(c: Candidate, ctx: Context) -> dict[str, Any]:
    req, k, now = ctx.req, ctx.req.constraints, ctx.now
    ev: list[dict[str, str]] = []
    missing: list[str] = []
    worst: list[str] = []

    def fail(status: str, rule: str, detail: str) -> None:
        ev.append(_ev(rule, "FAIL", detail))
        worst.append(status)

    def unknown(rule: str, detail: str, what: str) -> None:
        ev.append(_ev(rule, "UNKNOWN", detail))
        missing.append(what)
        worst.append("MISSING_INFORMATION")

    def ok(rule: str, detail: str) -> None:
        ev.append(_ev(rule, "PASS", detail))

    # structural rules --------------------------------------------------------------------------------------------------
    if c.merged:
        fail("DISQUALIFIED", "supplier_registry", "the supplier record was merged into another supplier")
    elif c.supplier_status == "INACTIVE":
        fail("DISQUALIFIED", "supplier_registry", "the supplier is inactive")
    elif c.supplier_status == "ON_HOLD":
        fail("UNAVAILABLE", "supplier_registry", "the supplier is on hold")
    else:
        ok("supplier_registry", "the supplier is active")
    if not ctx.product_ok:
        fail("DISQUALIFIED", "product_registry", ctx.product_note)
    if not ctx.destination_ok:
        fail("UNAVAILABLE", "destination_facility", ctx.destination_note)
    if c.unavailable_reason:
        fail("UNAVAILABLE", "scenario_availability", c.unavailable_reason)

    # supplier-product relationship ---------------------------------------------------------------------------------------
    if c.link == "CONFIRMED":
        ok("supplier_product_relationship", "a confirmed SUPPLIES_PRODUCT relationship exists")
    elif c.link in ("INACTIVE", "REJECTED"):
        fail("DISQUALIFIED", "supplier_product_relationship", f"the supplier-product relationship is {c.link.lower()}")
    elif c.link == "PROPOSED":
        unknown(
            "supplier_product_relationship",
            "the relationship is proposed, not yet confirmed",
            "confirmed supplier-product relationship",
        )
    else:
        unknown(
            "supplier_product_relationship",
            "no supplier-product relationship is registered",
            "supplier-product relationship",
        )

    # the commercial offer ---------------------------------------------------------------------------------------------
    if not c.has_offer:
        unknown(
            "commercial_offer",
            "no commercial offer (price, capacity, terms) is registered for this destination",
            "commercial offer: price, capacity, approval, lead time",
        )
        return _result(ev, missing, worst)
    if c.offer_status == "SUSPENDED":
        fail("UNAVAILABLE", "offer_status", "the offer is suspended")
    if c.approval == "APPROVED":
        ok("approval", "the supplier is approved for this product and destination")
    elif c.approval == "PENDING_QUALIFICATION":
        ev.append(_ev("approval", "FAIL", "qualification is still pending"))
        worst.append("ELIGIBLE_AFTER_QUALIFICATION")
    elif c.approval == "DISQUALIFIED":
        fail("DISQUALIFIED", "approval", "the supplier is disqualified for this product and destination")
    else:
        unknown("approval", "approval status is not recorded", "approval status")
    # contract window
    if c.contract_to is not None and c.contract_to < now:
        fail("DISQUALIFIED", "contract", f"the contract ended {c.contract_to.date().isoformat()}")
    elif c.contract_from is not None and c.contract_from > now:
        fail("UNAVAILABLE", "contract", f"the contract starts {c.contract_from.date().isoformat()}")
    elif c.contract_from is None and c.contract_to is None:
        ev.append(_ev("contract", "PASS", "no contract window is recorded (not verified)"))
    else:
        ok("contract", "the contract window includes the as-of date")
    # unit / currency / price / capacity
    if c.unit is None:
        unknown("unit", "the offer states no unit of measure", "offer unit")
    elif c.unit != req.unit:
        unknown(
            "unit",
            f"the offer is in {c.unit}, the requirement in {req.unit}; no conversion is applied",
            f"offer in {req.unit}",
        )
    else:
        ok("unit", f"quantities are in {req.unit}")
    if c.price is None or c.currency is None:
        unknown("price", "no unit price is recorded", "unit price and currency")
    elif c.currency != req.currency:
        unknown(
            "currency",
            f"the price is in {c.currency}, the requirement in {req.currency}; no exchange rate is applied",
            f"price in {req.currency}",
        )
    else:
        ok("price", "a unit price in the requirement currency is recorded")
    if c.capacity is None:
        unknown("capacity", "no capacity is recorded", "available capacity")
    elif c.effective_capacity is not None and c.effective_capacity <= 0:
        fail("UNAVAILABLE", "capacity", "the available capacity is zero")
    else:
        ok("capacity", f"capacity {c.effective_capacity:g} {req.unit} is recorded")
    if c.moq is not None and c.effective_capacity is not None and c.moq > c.effective_capacity:
        fail("UNAVAILABLE", "minimum_order_quantity", "the minimum order quantity exceeds the available capacity")
    # request filters ----------------------------------------------------------------------------------------------------
    if k.allowed_supplier_ids and c.supplier_id not in k.allowed_supplier_ids:
        fail("EXCLUDED", "approved_supplier_list", "the supplier is not on the allowed list")
    if c.supplier_id in k.excluded_supplier_ids:
        fail("EXCLUDED", "excluded_suppliers", "the supplier is on the excluded list")
    if c.country and c.country.lower() in {x.lower() for x in k.excluded_countries}:
        fail("EXCLUDED", "geographic_restriction", f"suppliers in {c.country} are excluded")
    limit = k.max_lead_time_days
    if k.required_by is not None:
        days = (k.required_by - now).total_seconds() / 86400.0
        limit = days if limit is None else min(limit, days)
    if limit is not None:
        if c.lead_days is None:
            unknown("lead_time", "no lead time is recorded but a limit applies", "lead time")
        elif c.lead_days > limit:
            fail("EXCLUDED", "lead_time", f"lead time {c.lead_days:g} days exceeds the {limit:g}-day limit")
        else:
            ok("lead_time", f"lead time {c.lead_days:g} days is within the {limit:g}-day limit")
    if k.max_defect_rate is not None:
        if c.defect_rate is None:
            unknown("quality", "no defect rate is recorded but a threshold applies", "defect rate")
        elif c.defect_rate > k.max_defect_rate:
            fail("EXCLUDED", "quality", f"defect rate {c.defect_rate:.3f} exceeds {k.max_defect_rate:.3f}")
        else:
            ok("quality", "the defect rate is within the threshold")
    if k.max_risk is not None:
        if c.risk is None:
            unknown(
                "supplier_risk",
                "no current risk assessment exists but a threshold applies",
                "current supplier risk assessment",
            )
        elif c.risk > k.max_risk:
            fail("EXCLUDED", "supplier_risk", f"risk score {c.risk:.2f} exceeds {k.max_risk:.2f}")
        else:
            ok("supplier_risk", "the risk score is within the threshold")
    if k.min_reliability is not None:
        if c.reliability.value is None:
            unknown(
                "delivery_reliability",
                f"fewer than {ctx.reliability_min_sample} measured deliveries; a minimum applies",
                "measured delivery reliability",
            )
        elif c.reliability.value < k.min_reliability:
            fail(
                "EXCLUDED",
                "delivery_reliability",
                f"measured on-time share {c.reliability.value:.2f} is below {k.min_reliability:.2f}",
            )
        else:
            ok("delivery_reliability", "measured on-time share meets the minimum")
    if k.max_data_age_days is not None:
        ref = c.observed_at
        age = None if ref is None else (now - ref).total_seconds() / 86400.0
        if age is None:
            unknown("data_freshness", "the observation date of the terms is not recorded", "observation date")
        elif age > k.max_data_age_days:
            unknown(
                "data_freshness",
                f"the terms are {age:.0f} days old (limit {k.max_data_age_days:g})",
                "fresher commercial data",
            )
        else:
            ok("data_freshness", f"the terms are {age:.0f} days old")
    if k.require_transport_route:
        st = (c.transport or {}).get("status", "NOT_EVALUATED")
        if st == "REACHABLE":
            ok("transport_route", "a registered route reaches the destination")
        elif st == "NOT_REACHABLE":
            fail("EXCLUDED", "transport_route", "no registered route reaches the destination")
        else:
            unknown(
                "transport_route",
                (c.transport or {}).get("note", "transport was not evaluated"),
                "registered transport route",
            )
    return _result(ev, missing, worst)


def _result(ev: list[dict[str, str]], missing: list[str], worst: list[str]) -> dict[str, Any]:
    status = max(worst, key=lambda s: SEVERITY[s]) if worst else "ELIGIBLE"
    reasons = [e["detail"] for e in ev if e["outcome"] != "PASS"]
    return {"status": status, "evidence": ev, "reasons": reasons, "missing": sorted(set(missing))}
