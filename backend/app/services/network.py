"""Network overview and the DB -> NetworkData loader for deterministic calculations."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import EntityType, Provenance
from app.models.facility import Facility
from app.models.observation import Observation
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from app.schemas.network import NetworkNode, NetworkSummary
from app.services.mappers import relationship_read
from app.services.queries import _rel_query
from app.services.risk import FacilityRec, NetworkData, ObsRec, RelRec, SupplierRec

NODE_LIMIT = 1000
DEFAULT_EDGE_LIMIT = 500
MAX_EDGE_LIMIT = 2000


def provenance_counts(session: Session) -> dict[str, int]:
    counts = {p.value: 0 for p in Provenance}
    for model in (Supplier, Facility, SupplyRelationship, Observation):
        for prov, n in session.execute(select(model.provenance, func.count()).group_by(model.provenance)):
            counts[prov.value] += n
    return counts


def load_network_data(session: Session) -> NetworkData:
    """Load all rows needed for the baseline metrics (compact tuples; fine for scale)."""
    return NetworkData(
        suppliers=[
            SupplierRec(i, c, p.value)
            for i, c, p in session.execute(select(Supplier.id, Supplier.supplier_code, Supplier.provenance))
        ],
        facilities=[
            FacilityRec(i, c, p.value)
            for i, c, p in session.execute(select(Facility.id, Facility.facility_code, Facility.provenance))
        ],
        relationships=[
            RelRec(t, s, sf, tf, cv, cu, lt, p.value)
            for t, s, sf, tf, cv, cu, lt, p in session.execute(
                select(
                    SupplyRelationship.relationship_type,
                    SupplyRelationship.supplier_id,
                    SupplyRelationship.source_facility_id,
                    SupplyRelationship.target_facility_id,
                    SupplyRelationship.capacity_value,
                    SupplyRelationship.capacity_unit,
                    SupplyRelationship.lead_time_days,
                    SupplyRelationship.provenance,
                )
            )
        ],
        observations=[
            ObsRec(s, f, p.value)
            for s, f, p in session.execute(
                select(Observation.supplier_id, Observation.facility_id, Observation.provenance)
            )
        ],
    )


def build_network_summary(session: Session, max_edges: int = DEFAULT_EDGE_LIMIT) -> NetworkSummary:
    supplier_count = session.scalar(select(func.count()).select_from(Supplier)) or 0
    facility_count = session.scalar(select(func.count()).select_from(Facility)) or 0
    relationship_count = session.scalar(select(func.count()).select_from(SupplyRelationship)) or 0
    suppliers = session.scalars(select(Supplier).order_by(Supplier.supplier_code).limit(NODE_LIMIT)).all()
    facilities = session.scalars(select(Facility).order_by(Facility.facility_code).limit(NODE_LIMIT)).all()
    edges = session.scalars(_rel_query().order_by(SupplyRelationship.id).limit(max_edges)).unique().all()
    nodes = [
        NetworkNode(
            type=EntityType.SUPPLIER,
            id=s.id,
            code=s.supplier_code,
            name=s.name,
            status=s.status,
            provenance=s.provenance,
        )
        for s in suppliers
    ]
    nodes += [
        NetworkNode(
            type=EntityType.FACILITY,
            id=f.id,
            code=f.facility_code,
            name=f.name,
            status=f.status,
            provenance=f.provenance,
        )
        for f in facilities
    ]
    return NetworkSummary(
        supplier_count=supplier_count,
        facility_count=facility_count,
        relationship_count=relationship_count,
        nodes=nodes,
        edges=[relationship_read(e) for e in edges],
        edges_returned=len(edges),
        truncated=len(edges) < relationship_count
        or len(suppliers) < supplier_count
        or len(facilities) < facility_count,
        provenance_counts=provenance_counts(session),
    )
