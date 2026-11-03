"""Closed relationship vocabulary, endpoint rules, direction semantics and hard query limits."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.models.enums import RegistryType


class GraphRelType(str, Enum):
    SUPPLIES = "SUPPLIES"  # supplier -> facility
    TRANSFERS_TO = "TRANSFERS_TO"  # facility -> facility
    SUPPLIES_PRODUCT = "SUPPLIES_PRODUCT"  # supplier -> product
    PRODUCES = "PRODUCES"  # facility -> product
    STORES = "STORES"  # facility -> product
    OWNS = "OWNS"  # supplier -> facility (derived from facilities.owner_supplier_id; read-only)


class EdgeKind(str, Enum):
    """Which table stores the relationship."""

    SUPPLY = "SUPPLY"  # supply_relationships
    PRODUCT = "PRODUCT"  # product_links
    OWNER = "OWNER"  # facilities.owner_supplier_id (derived)


@dataclass(frozen=True)
class TypeRule:
    type: GraphRelType
    source: RegistryType
    destination: RegistryType
    kind: EdgeKind
    editable: bool
    cycles: str  # "impossible" (bipartite by construction) | "allowed" (legitimate; reported as a warning)
    meaning: str


S, F, P = RegistryType.SUPPLIER, RegistryType.FACILITY, RegistryType.PRODUCT

RULES: dict[GraphRelType, TypeRule] = {
    GraphRelType.SUPPLIES: TypeRule(
        GraphRelType.SUPPLIES,
        S,
        F,
        EdgeKind.SUPPLY,
        True,
        "impossible",
        "The supplier ships goods or services to the facility.",
    ),
    GraphRelType.TRANSFERS_TO: TypeRule(
        GraphRelType.TRANSFERS_TO,
        F,
        F,
        EdgeKind.SUPPLY,
        True,
        "allowed",
        "The source facility ships goods to the destination facility (inter-site transfer).",
    ),
    GraphRelType.SUPPLIES_PRODUCT: TypeRule(
        GraphRelType.SUPPLIES_PRODUCT,
        S,
        P,
        EdgeKind.PRODUCT,
        True,
        "impossible",
        "The supplier supplies the product.",
    ),
    GraphRelType.PRODUCES: TypeRule(
        GraphRelType.PRODUCES,
        F,
        P,
        EdgeKind.PRODUCT,
        True,
        "impossible",
        "The facility manufactures the product.",
    ),
    GraphRelType.STORES: TypeRule(
        GraphRelType.STORES,
        F,
        P,
        EdgeKind.PRODUCT,
        True,
        "impossible",
        "The facility holds the product in storage.",
    ),
    GraphRelType.OWNS: TypeRule(
        GraphRelType.OWNS,
        S,
        F,
        EdgeKind.OWNER,
        False,
        "impossible",
        "The supplier is the registered owner of the facility (read-only; edit the facility's owner in the registry).",
    ),
}
# Edges point in the direction of material/commercial flow. "Downstream" of a node follows edges away from it
# (source -> destination); "upstream" follows edges towards it. This holds for every type above.
NOT_MODELLED = {
    "DEPENDS_ON": "Product-to-product (component) dependencies are not modelled in the ATLAS domain yet.",
    "OPERATES": "No operator concept exists; OWNS is derived from the facility owner field instead.",
    "SHIPS_TO": "Named TRANSFERS_TO in the existing domain model.",
}


@dataclass(frozen=True)
class Limits:
    """Server-side hard caps. Requests above them are rejected with a validation error, never silently raised."""

    max_depth: int = 6
    default_depth: int = 2
    max_nodes: int = 500
    default_nodes: int = 200
    max_edges: int = 1000
    default_edges: int = 400
    max_paths: int = 10
    default_paths: int = 3
    max_path_length: int = 8
    default_path_length: int = 6
    max_partial_paths_per_level: int = 5000
    scan_cap_components: int = 50_000  # edges loaded for connected components
    scan_cap_integrity: int = 200_000  # edges examined per integrity run
    integrity_issue_cap: int = 300
    time_budget_seconds: float = 5.0
    in_chunk: int = 400  # IN-list chunk size


LIMITS = Limits()

ENDPOINT_COLUMNS = {  # for documentation only; real enforcement is the CHECK constraints plus service validation
    GraphRelType.SUPPLIES: ("supplier_id", "target_facility_id"),
    GraphRelType.TRANSFERS_TO: ("source_facility_id", "target_facility_id"),
    GraphRelType.SUPPLIES_PRODUCT: ("supplier_id", "product_id"),
    GraphRelType.PRODUCES: ("facility_id", "product_id"),
    GraphRelType.STORES: ("facility_id", "product_id"),
    GraphRelType.OWNS: ("owner_supplier_id", "id"),
}
