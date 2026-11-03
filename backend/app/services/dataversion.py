"""Cheap 'has the network data changed?' token used to validate cached analytics.

One statement with scalar sub-selects (index-only for the count/max(id) parts). It changes on insert, delete and any ORM update
(``updated_at`` is maintained by ``onupdate``). A raw-SQL update that does not touch ``updated_at`` is not detected until the TTL
expires; that bound is documented in CACHING_STRATEGY.md.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.facility import Facility
from app.models.observation import Observation
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier


def network_data_version(session: Session) -> tuple:
    row = session.execute(
        select(
            select(func.count()).select_from(Supplier).scalar_subquery(),
            select(func.max(Supplier.updated_at)).scalar_subquery(),
            select(func.count()).select_from(Facility).scalar_subquery(),
            select(func.max(Facility.updated_at)).scalar_subquery(),
            select(func.count()).select_from(SupplyRelationship).scalar_subquery(),
            select(func.max(SupplyRelationship.updated_at)).scalar_subquery(),
            select(func.count()).select_from(Observation).scalar_subquery(),
            select(func.max(Observation.id)).scalar_subquery(),
        )
    ).one()
    return tuple(row)
