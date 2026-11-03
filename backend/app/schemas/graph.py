"""Request and response models for the network graph API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.graph.rules import GraphRelType
from app.models.enums import EdgeCriticality, EdgeStatus, RegistryType

Num = Annotated[float, Field(allow_inf_nan=False)]
Reason = Annotated[str, Field(min_length=3, max_length=500)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Attributes(_Strict):
    capacity_value: Annotated[float, Field(ge=0, le=1e15, allow_inf_nan=False)] | None = None
    capacity_unit: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    lead_time_days: Annotated[float, Field(ge=0, le=3650, allow_inf_nan=False)] | None = None
    supply_share_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] | None = None
    criticality: EdgeCriticality | None = None
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    @model_validator(mode="after")
    def _pairs(self) -> _Attributes:
        if (self.capacity_value is None) != (self.capacity_unit is None):
            raise ValueError("capacity_value and capacity_unit must be given together (or both left unknown).")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not be before effective_from.")
        return self


class RelationshipCreate(_Attributes):
    type: GraphRelType
    source_type: RegistryType
    source_id: Annotated[int, Field(ge=1, le=2**31 - 1)]
    destination_type: RegistryType
    destination_id: Annotated[int, Field(ge=1, le=2**31 - 1)]
    status: Literal[EdgeStatus.PROPOSED, EdgeStatus.CONFIRMED] = EdgeStatus.PROPOSED
    justification: Reason = Field(description="Why this relationship is believed to exist (kept in its history).")


class RelationshipUpdate(_Attributes):
    """Only attributes that appear in the body are changed; an explicit null marks a value as unknown again."""

    expected_version: Annotated[int, Field(ge=1)]
    reason: Reason

    @field_validator("capacity_unit")
    @classmethod
    def _unit(cls, v: str | None) -> str | None:
        return v


class StatusChange(_Strict):
    status: EdgeStatus
    reason: Reason
    expected_version: Annotated[int, Field(ge=1)]


# ----------------------------------------------------------------------------------------------- responses


class NodeOut(BaseModel):
    type: RegistryType
    id: int
    code: str
    name: str
    lifecycle: str
    detail: str | None = None
    provenance: str
    depth: int | None = None


class EdgeOut(BaseModel):
    ref: str
    type: GraphRelType
    source_type: RegistryType
    source_id: int
    destination_type: RegistryType
    destination_id: int
    status: EdgeStatus
    status_reason: str | None = None
    code: str
    provenance: str
    source_namespace: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    capacity_value: float | None = None
    capacity_unit: str | None = None
    lead_time_days: float | None = None
    supply_share_pct: float | None = None
    criticality: EdgeCriticality | None = None
    confidence: float | None = None
    version: int | None = None
    read_only: bool = False
    hop: int | None = None
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GraphOut(BaseModel):
    """A bounded subgraph. ``truncated`` is true whenever any cap stopped the result from being complete."""

    start: NodeOut | None = None
    direction: str | None = None
    nodes: list[NodeOut]
    edges: list[EdgeOut]
    nodes_returned: int
    edges_returned: int
    requested_depth: int | None = None
    max_depth_reached: int | None = None
    nodes_at_depth_limit: int | None = None
    truncated: bool
    truncation_reasons: list[str] = []
    limits: dict[str, Any] = {}
    resolved_from: dict[str, Any] | None = None


class PathOut(BaseModel):
    length: int
    nodes: list[NodeOut]
    edges: list[EdgeOut]
    relationship_types: list[GraphRelType]
    provenance: list[str]
    statuses: list[EdgeStatus]


class PathsOut(BaseModel):
    start: NodeOut
    goal: NodeOut
    mode: str
    paths: list[PathOut]
    found: bool
    shortest_length: int | None
    truncated: bool
    truncation_reasons: list[str] = []
    note: str
    resolved_from: dict[str, Any] | None = None
