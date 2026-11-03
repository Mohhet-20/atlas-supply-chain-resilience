"""Best-effort targeted reassessment after relevant edits. A failure here never fails the user's write.

Only the suppliers an edit can change are reassessed: the supplier on a relationship plus the other suppliers of the same
product or facility (their concentration and single-source status depend on it). The set is capped, and the whole
registry is never recalculated for a small edit.
"""

from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy import select

from app.db.tenancy import request_session_factory
from app.graph.store import parse_ref
from app.models.enums import EntityKind, ProductLinkType, RelationshipType, RiskRunScope
from app.models.ingestion import RecordLineage
from app.models.product import ProductLink
from app.models.relationship import SupplyRelationship
from app.risk import runs
from app.risk.methodology import risk_config

logger = logging.getLogger("atlas.risk")
MAX_AFFECTED = 25


def _enabled(request: Request) -> bool:
    return bool(request.app.state.settings.risk_auto_reassess)


def affected_by_edge(session, ref: str) -> list[int]:
    kind, row_id = parse_ref(ref)
    if kind.value == "SUPPLY":
        r = session.get(SupplyRelationship, row_id)
        if r is None or r.relationship_type is not RelationshipType.SUPPLIES:
            return []
        ids = {r.supplier_id} | set(session.scalars(
            select(SupplyRelationship.supplier_id).where(
                SupplyRelationship.relationship_type == RelationshipType.SUPPLIES, SupplyRelationship.target_facility_id == r.target_facility_id
            )
        ))  # fmt: skip
    elif kind.value == "PRODUCT":
        lk = session.get(ProductLink, row_id)
        if lk is None or lk.link_type is not ProductLinkType.SUPPLIES_PRODUCT:
            return []
        ids = {lk.supplier_id} | set(session.scalars(
            select(ProductLink.supplier_id).where(ProductLink.link_type == ProductLinkType.SUPPLIES_PRODUCT, ProductLink.product_id == lk.product_id)
        ))  # fmt: skip
    else:
        return []
    return sorted(i for i in ids if i is not None)[:MAX_AFFECTED]


def reassess_suppliers(request: Request, supplier_ids: list[int], actor: str) -> None:
    if not _enabled(request) or not supplier_ids:
        return
    s = request.app.state.settings
    ids = sorted(set(supplier_ids))[:MAX_AFFECTED]
    try:
        scope = RiskRunScope.SUPPLIER if len(ids) == 1 else RiskRunScope.SUPPLIERS
        ref = {"supplier_id": ids[0]} if len(ids) == 1 else {"supplier_ids": ids}
        runs.assess_now(request_session_factory(request), scope, ref, f"auto:{actor}"[:64], risk_config(s.risk_config_path), trigger="AUTO",
                        limits=runs.limits_from(s))  # fmt: skip
    except Exception:  # noqa: BLE001 - includes "already running"; the next run catches up
        logger.warning("risk reassessment skipped", extra={"suppliers": len(ids)}, exc_info=False)


def reassess_edge(request: Request, ref: str, actor: str) -> None:
    if not _enabled(request):
        return
    try:
        with request_session_factory(request)() as session:
            ids = affected_by_edge(session, ref)
    except Exception:  # noqa: BLE001
        return
    reassess_suppliers(request, ids, actor)


def affected_by_supplier(session, supplier_id: int) -> list[int]:
    """The supplier plus the other suppliers of any product or facility it serves (a region or status edit changes them)."""
    fac = list(session.scalars(select(SupplyRelationship.target_facility_id).where(
        SupplyRelationship.relationship_type == RelationshipType.SUPPLIES, SupplyRelationship.supplier_id == supplier_id)))  # fmt: skip
    prod = list(session.scalars(select(ProductLink.product_id).where(
        ProductLink.link_type == ProductLinkType.SUPPLIES_PRODUCT, ProductLink.supplier_id == supplier_id)))  # fmt: skip
    ids = {supplier_id}
    if fac:
        ids |= set(session.scalars(select(SupplyRelationship.supplier_id).where(
            SupplyRelationship.relationship_type == RelationshipType.SUPPLIES, SupplyRelationship.target_facility_id.in_(fac))))  # fmt: skip
    if prod:
        ids |= set(session.scalars(select(ProductLink.supplier_id).where(
            ProductLink.link_type == ProductLinkType.SUPPLIES_PRODUCT, ProductLink.product_id.in_(prod))))  # fmt: skip
    return sorted(i for i in ids if i is not None)[:MAX_AFFECTED]


def reassess_supplier_edit(request: Request, supplier_id: int, actor: str) -> None:
    if not _enabled(request):
        return
    try:
        with request_session_factory(request)() as session:
            ids = affected_by_supplier(session, supplier_id)
    except Exception:  # noqa: BLE001
        return
    reassess_suppliers(request, ids, actor)


def reassess_batch(request: Request, batch_id: int, actor: str) -> None:
    """After an import: reassess the suppliers it touched, but only when that is a small set.

    A large import is deliberately not reassessed here (that would be a registry-wide recalculation after one request);
    the overview reports that data changed since the last network run so an analyst can queue one.
    """
    if not _enabled(request):
        return
    try:
        with request_session_factory(request)() as session:
            rows = session.execute(
                select(RecordLineage.entity_type, RecordLineage.entity_id)
                .where(RecordLineage.batch_id == batch_id)
                .distinct()
            ).all()
            ids: set[int] = set()
            for kind, eid in rows:
                if kind is EntityKind.SUPPLIER:
                    ids.add(eid)
                elif kind is EntityKind.RELATIONSHIP:
                    ids.update(affected_by_edge(session, f"SUPPLY:{eid}"))
                elif kind is EntityKind.PRODUCT_LINK:
                    ids.update(affected_by_edge(session, f"PRODUCT:{eid}"))
    except Exception:  # noqa: BLE001
        return
    if not ids:
        return
    if len(ids) > MAX_AFFECTED:
        logger.info("import touched %s suppliers; automatic reassessment skipped (run a network assessment)", len(ids))
        return
    reassess_suppliers(request, sorted(ids), actor)
