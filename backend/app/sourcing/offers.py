"""Offer registration and availability. An offer's terms are immutable; a changed price or capacity is a new offer."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.enums import EntityStatus, Provenance
from app.models.facility import Facility
from app.models.product import Product
from app.models.sourcing import SourcingOffer
from app.models.supplier import Supplier
from app.models.types import utcnow

Quality = Literal["VERIFIED", "ESTIMATED", "UNKNOWN"]
Tag = Annotated[str, Field(pattern=r"^[A-Z0-9_]{2,40}$")]
PRICE_KEYS = ("unit_price", "freight_per_unit")


class OfferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: int = Field(ge=1, le=2**31 - 1)
    product_id: int = Field(ge=1, le=2**31 - 1)
    destination_facility_id: int = Field(ge=1, le=2**31 - 1)
    approval_status: Literal["APPROVED", "PENDING_QUALIFICATION", "DISQUALIFIED", "UNKNOWN"] = "UNKNOWN"
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    unit_price: float | None = Field(default=None, ge=0, le=1e12)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    price_quality: Quality = "UNKNOWN"
    freight_per_unit: float | None = Field(default=None, ge=0, le=1e12)
    capacity_amount: float | None = Field(default=None, ge=0, le=1e12)
    capacity_quality: Quality = "UNKNOWN"
    capacity_observed_at: datetime | None = None
    min_order_quantity: float | None = Field(default=None, gt=0, le=1e12)
    lot_size: float | None = Field(default=None, gt=0, le=1e12)
    max_allocation: float | None = Field(default=None, ge=0, le=1e12)
    lead_time_days: float | None = Field(default=None, ge=0, le=3650)
    lead_time_std_days: float | None = Field(default=None, ge=0, le=3650)
    lead_time_quality: Quality = "UNKNOWN"
    defect_rate: float | None = Field(default=None, ge=0, le=1)
    quality_quality: Quality = "UNKNOWN"
    contract_valid_from: datetime | None = None
    contract_valid_to: datetime | None = None
    restrictions: list[Tag] = Field(default_factory=list, max_length=20)
    provenance: Literal["SYNTHETIC_DEMO", "USER_PROVIDED", "IMPORTED_EXTERNAL", "MODEL_ESTIMATE", "SIMULATED"] = (
        "USER_PROVIDED"
    )
    source_note: str | None = Field(default=None, max_length=300)
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> OfferIn:
        if (self.unit_price is None) != (self.currency is None):
            raise ValueError("unit_price and currency must be given together.")
        if self.freight_per_unit is not None and self.unit_price is None:
            raise ValueError("freight_per_unit needs a unit_price in the same currency.")
        for q, v in (("price_quality", self.unit_price), ("capacity_quality", self.capacity_amount), ("lead_time_quality", self.lead_time_days), ("quality_quality", self.defect_rate)):  # fmt: skip
            if v is None and getattr(self, q) != "UNKNOWN":
                raise ValueError(f"{q} must be UNKNOWN when the value is not given.")
        if (
            self.capacity_amount is not None
            or self.min_order_quantity
            or self.lot_size
            or self.max_allocation is not None
        ) and self.unit is None:
            raise ValueError(
                "A unit is required when a capacity, minimum order quantity, order increment or maximum allocation is given."
            )
        if (
            self.min_order_quantity
            and self.capacity_amount is not None
            and self.min_order_quantity > self.capacity_amount > 0
        ):
            pass  # stated as is; eligibility reports it as unavailable
        for n in ("observed_at", "capacity_observed_at", "contract_valid_from", "contract_valid_to"):
            v = getattr(self, n)
            if v is not None and v.tzinfo is None:
                raise ValueError(f"{n} must include a timezone offset.")
        if self.contract_valid_from and self.contract_valid_to and self.contract_valid_to < self.contract_valid_from:
            raise ValueError("contract_valid_to must not be before contract_valid_from.")
        return self


def create(session: Session, body: OfferIn, actor: str) -> SourcingOffer:
    s = session.get(Supplier, body.supplier_id)
    if s is None:
        raise NotFoundError(f"Supplier {body.supplier_id} does not exist.")
    if s.merged_into_id is not None:
        raise ConflictError(
            f"Supplier {body.supplier_id} was merged into {s.merged_into_id}; register the offer on the surviving supplier."
        )
    p = session.get(Product, body.product_id)
    if p is None:
        raise NotFoundError(f"Product {body.product_id} does not exist.")
    if p.merged_into_id is not None or p.retired_at is not None:
        raise ConflictError(f"Product {body.product_id} is retired or merged.")
    f = session.get(Facility, body.destination_facility_id)
    if f is None:
        raise NotFoundError(f"Destination facility {body.destination_facility_id} does not exist.")
    if f.merged_into_id is not None or f.status is not EntityStatus.ACTIVE:
        raise ConflictError(f"Destination facility {body.destination_facility_id} is not an active facility.")
    row = SourcingOffer(
        offer_uid=f"OF-{uuid.uuid4().hex[:16].upper()}", supplier_id=body.supplier_id, product_id=body.product_id,
        destination_facility_id=body.destination_facility_id, approval_status=body.approval_status, unit=body.unit, unit_price=body.unit_price,
        currency=body.currency, price_quality=body.price_quality, freight_per_unit=body.freight_per_unit, capacity_amount=body.capacity_amount,
        capacity_quality=body.capacity_quality, capacity_observed_at=body.capacity_observed_at, min_order_quantity=body.min_order_quantity,
        lot_size=body.lot_size, max_allocation=body.max_allocation, lead_time_days=body.lead_time_days, lead_time_std_days=body.lead_time_std_days,
        lead_time_quality=body.lead_time_quality, defect_rate=body.defect_rate, quality_quality=body.quality_quality,
        contract_valid_from=body.contract_valid_from, contract_valid_to=body.contract_valid_to, restrictions=sorted(set(body.restrictions)),
        status="ACTIVE", provenance=Provenance(body.provenance), source_note=body.source_note, observed_at=body.observed_at, created_by=actor, created_at=utcnow(),
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


def get(session: Session, uid: str) -> SourcingOffer:
    row = session.scalar(select(SourcingOffer).where(SourcingOffer.offer_uid == uid))
    if row is None:
        raise NotFoundError("Offer not found.")
    return row


_ALLOWED = {("ACTIVE", "SUSPENDED"), ("SUSPENDED", "ACTIVE"), ("ACTIVE", "RETIRED"), ("SUSPENDED", "RETIRED")}


def set_status(row: SourcingOffer, to: str) -> SourcingOffer:
    if (row.status, to) not in _ALLOWED:
        raise ConflictError(
            f"An offer cannot move from {row.status} to {to}."
            + (" Retired offers are final." if row.status == "RETIRED" else "")
        )
    row.status = to
    if to == "RETIRED":
        row.retired_at = utcnow()
    return row


def out(r: SourcingOffer, redact: bool = False) -> dict[str, Any]:
    iso = lambda d: None if d is None else d.isoformat()  # noqa: E731
    d = {
        "offer_uid": r.offer_uid, "supplier_id": r.supplier_id, "product_id": r.product_id, "destination_facility_id": r.destination_facility_id,
        "approval_status": r.approval_status, "unit": r.unit, "unit_price": r.unit_price, "currency": r.currency, "price_quality": r.price_quality,
        "freight_per_unit": r.freight_per_unit, "capacity_amount": r.capacity_amount, "capacity_quality": r.capacity_quality,
        "capacity_observed_at": iso(r.capacity_observed_at), "min_order_quantity": r.min_order_quantity, "lot_size": r.lot_size,
        "max_allocation": r.max_allocation, "lead_time_days": r.lead_time_days, "lead_time_std_days": r.lead_time_std_days,
        "lead_time_quality": r.lead_time_quality, "defect_rate": r.defect_rate, "quality_quality": r.quality_quality,
        "contract_valid_from": iso(r.contract_valid_from), "contract_valid_to": iso(r.contract_valid_to), "restrictions": list(r.restrictions or []),
        "status": r.status, "provenance": r.provenance.value, "source_note": r.source_note, "observed_at": iso(r.observed_at),
        "created_by": r.created_by, "created_at": iso(r.created_at), "retired_at": iso(r.retired_at),
    }  # fmt: skip
    if redact:
        for k in PRICE_KEYS:
            d[k] = None
        d["price_redacted"] = True
    return d
