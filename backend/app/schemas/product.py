from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import EntityStatus, ProductLinkType, Provenance
from app.schemas.common import ORMModel, validate_code


class ProductCreate(BaseModel):
    product_code: str = Field(max_length=48)
    name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    unit_of_measure: str | None = Field(default=None, max_length=32)
    status: EntityStatus = EntityStatus.ACTIVE
    provenance: Provenance

    @field_validator("product_code")
    @classmethod
    def _code(cls, v: str) -> str:
        return validate_code(v, 48)


class ProductLinkCreate(BaseModel):
    """SUPPLIES_PRODUCT = supplier_code -> product_code; PRODUCES/STORES = facility_code -> product_code."""

    link_code: str = Field(max_length=48)
    link_type: ProductLinkType
    product_code: str
    supplier_code: str | None = None
    facility_code: str | None = None
    quantity_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    quantity_unit: str | None = Field(default=None, max_length=32)
    lead_time_days: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    effective_from: date | None = None
    effective_to: date | None = None
    provenance: Provenance

    @field_validator("link_code")
    @classmethod
    def _code(cls, v: str) -> str:
        return validate_code(v, 48)

    @model_validator(mode="after")
    def _endpoints(self) -> ProductLinkCreate:
        if self.link_type is ProductLinkType.SUPPLIES_PRODUCT:
            if not self.supplier_code or self.facility_code:
                raise ValueError("SUPPLIES_PRODUCT requires supplier_code and forbids facility_code")
        elif not self.facility_code or self.supplier_code:
            raise ValueError(f"{self.link_type.value} requires facility_code and forbids supplier_code")
        if (self.quantity_value is None) != (self.quantity_unit is None):
            raise ValueError("quantity_value and quantity_unit must be provided together")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not be before effective_from")
        return self


class ProductRead(ORMModel):
    id: int
    product_code: str
    name: str
    category: str | None
    unit_of_measure: str | None
    status: EntityStatus
    provenance: Provenance
    source_namespace: str | None = None
    external_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ProductLinkRead(BaseModel):
    id: int
    link_code: str
    link_type: ProductLinkType
    product_code: str
    product_name: str
    supplier_code: str | None
    supplier_name: str | None
    facility_code: str | None
    facility_name: str | None
    quantity_value: float | None
    quantity_unit: str | None
    lead_time_days: float | None
    effective_from: date | None
    effective_to: date | None
    provenance: Provenance


class ProductDetail(ProductRead):
    links: list[ProductLinkRead]
