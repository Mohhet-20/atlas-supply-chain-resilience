"""SYNTHETIC fixtures for decision-support tests. Every run, cost and quantity is invented; results verify the software only."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.enums import SimStatus
from app.models.resilience import ResilienceRun

D = "/api/v1/decision"


def alt(aid: str, cost: float | None, unmet: float | None, scen: list[dict[str, Any]] | None = None, hhi: float | None = 0.5, **kw: Any) -> dict[str, Any]:  # fmt: skip
    return {"id": aid, "feasible": True, "allocation": [{"supplier_code": "S1", "quantity": 60}], "constraint_checks": [{"id": "CAP", "passed": True}],
            "cost": {"total_known": cost, "currency": "USD", "basis": "PURCHASE_ONLY"} if cost is not None else {},
            "values": {k: v for k, v in {"UNMET_DEMAND": unmet, "CONCENTRATION": hhi}.items() if v is not None},
            "baseline_fulfilment": None if unmet is None else 1 - unmet / 60, "scenarios": scen or [], **kw}  # fmt: skip


def scen(name: str, unmet: float, prob: float | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "unmet": unmet,
        "fulfilment": 1 - unmet / 60,
        "mode": "FIXED",
        "probability": prob,
        "kind": "SUPPLIER_OUTAGE",
    }


def run(db: Session, uid: str, alts: list[dict[str, Any]], qty: float = 60.0, product: int = 1, dest: int = 1, recommended: str | None = None, status: str = "completed") -> ResilienceRun:  # fmt: skip
    r = ResilienceRun(run_uid=uid, status=SimStatus.COMPLETED, result_status=status, requested_by="test", product_id=product, destination_facility_id=dest, strategy="PARETO",
                      scenario_mode="FIXED", request={"quantity": qty, "unit": "kg", "currency": "USD"}, request_hash="h" + uid, algorithm_version="rs-test",
                      alternatives=alts, recommendation={"id": recommended or alts[0]["id"]}, comparison={}, provenance={}, scenarios={"mode": "FIXED"})  # fmt: skip
    db.add(r)
    db.commit()
    return r


def definition(base: str, other: str, **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"title": "Dual sourcing study", "purpose": "Compare plans under a supplier outage",
                         "evidence": [{"kind": "RESILIENCE_RUN", "run_uid": base, "role": "BASELINE", "plan": "A"}, {"kind": "RESILIENCE_RUN", "run_uid": other, "role": "ALTERNATIVE", "plan": "B"}]}  # fmt: skip
    return d | kw
