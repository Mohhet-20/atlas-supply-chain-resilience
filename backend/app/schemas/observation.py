from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import EntityType, Provenance
from app.schemas.common import validate_code


class ObservationCreate(BaseModel):
    observation_code: str = Field(max_length=48)
    supplier_code: str | None = None
    facility_code: str | None = None
    metric_name: str = Field(min_length=1, max_length=100)
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=32)
    observed_at: datetime
    source_label: str = Field(min_length=1, max_length=200)
    provenance: Provenance
    quality_notes: str | None = Field(default=None, max_length=2000)

    @field_validator("observation_code")
    @classmethod
    def _code(cls, v: str) -> str:
        return validate_code(v, 48)

    @field_validator("metric_name", "unit", "source_label")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _one_subject(self) -> ObservationCreate:
        if bool(self.supplier_code) == bool(self.facility_code):
            raise ValueError("Exactly one of supplier_code or facility_code is required")
        return self


class SubjectRef(BaseModel):
    type: EntityType
    id: int
    code: str
    name: str


class ObservationRead(BaseModel):
    id: int
    observation_code: str
    subject: SubjectRef
    metric_name: str
    value: float
    unit: str
    observed_at: datetime
    source_label: str
    provenance: Provenance
    quality_notes: str | None
