from __future__ import annotations

from pydantic import BaseModel

from app.models.enums import EntityStatus, EntityType, Provenance
from app.schemas.relationship import RelationshipRead


class NetworkNode(BaseModel):
    type: EntityType
    id: int
    code: str
    name: str
    status: EntityStatus
    provenance: Provenance


class NetworkSummary(BaseModel):
    supplier_count: int
    facility_count: int
    relationship_count: int
    nodes: list[NetworkNode]
    edges: list[RelationshipRead]
    edges_returned: int
    truncated: bool
    provenance_counts: dict[str, int]
