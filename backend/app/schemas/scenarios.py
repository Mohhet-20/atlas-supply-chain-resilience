"""Typed request bodies for the scenario API. Responses are plain dicts built from persisted rows."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import EventType, RegistryType, ScenarioType

REF_RE = re.compile(r"^(SUPPLY|PRODUCT):[0-9]{1,10}$")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetRef(_Body):
    type: RegistryType
    id: int = Field(ge=1, le=2**31 - 1)


class EventBody(_Body):
    event_type: EventType
    label: str | None = Field(default=None, max_length=120)
    target_type: RegistryType | None = None
    target_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    relationship_ref: str | None = Field(default=None, max_length=32)
    targets: list[TargetRef] | None = Field(default=None, max_length=500)
    region_label: str | None = Field(default=None, max_length=120)
    effect: str | None = Field(default=None, pattern="^(UNAVAILABLE|REDUCTION)$")
    magnitude_pct: float | None = Field(
        default=None, ge=-1e9, le=1e9
    )  # range is checked by validation with a clear message
    added_lead_time_days: float | None = Field(default=None, ge=-1e9, le=1e9)
    start_offset_days: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    duration_days: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    recovery_days: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    assumption_notes: str | None = Field(default=None, max_length=1000)
    input_provenance: str | None = Field(default=None, max_length=300)

    @field_validator("relationship_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v is not None and not REF_RE.match(v):
            raise ValueError("relationship_ref must look like SUPPLY:12 or PRODUCT:7")
        return v

    def values(self) -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude_unset=True)
        return d


class EventPatch(EventBody):
    event_type: EventType | None = None  # type: ignore[assignment]


class ScenarioCreate(_Body):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    scenario_type: ScenarioType
    assumptions: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=5000)
    template_key: str | None = Field(default=None, max_length=48)
    events: list[EventBody] | None = Field(default=None, max_length=100)


class ScenarioPatch(_Body):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    scenario_type: ScenarioType | None = None
    assumptions: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=5000)
