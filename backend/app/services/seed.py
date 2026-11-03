"""Idempotent synthetic demo seeding. Never creates the schema, never deletes or overwrites rows."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, DomainValidationError
from app.db.migrations_check import assert_schema_current
from app.models.enums import Provenance, SourceStatus, SourceType
from app.models.observation import Observation
from app.models.product import Product, ProductLink
from app.models.relationship import SupplyRelationship
from app.models.source import DataSource
from app.schemas.facility import FacilityCreate
from app.schemas.observation import ObservationCreate
from app.schemas.product import ProductCreate, ProductLinkCreate
from app.schemas.relationship import RelationshipCreate
from app.schemas.supplier import SupplierCreate
from app.services import registry

logger = logging.getLogger("atlas.seed")


class SeedFile(BaseModel):
    dataset: str
    notice: str
    suppliers: list[SupplierCreate]
    facilities: list[FacilityCreate]
    relationships: list[RelationshipCreate]
    observations: list[ObservationCreate]
    products: list[ProductCreate] = []
    product_links: list[ProductLinkCreate] = []


DEMO_NAMESPACE = "atlas-demo"
KEYS = ("suppliers", "facilities", "relationships", "products", "product_links", "observations")


@dataclass
class SeedResult:
    created: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KEYS, 0))
    skipped_existing: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KEYS, 0))


def load_seed_file(path: Path) -> SeedFile:
    try:
        seed = SeedFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise DomainValidationError(f"Seed file not found: {path.name}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise DomainValidationError(f"Seed file is invalid: {type(exc).__name__}") from exc
    records = [
        *seed.suppliers,
        *seed.facilities,
        *seed.relationships,
        *seed.observations,
        *seed.products,
        *seed.product_links,
    ]
    if any(r.provenance is not Provenance.SYNTHETIC_DEMO for r in records):
        raise DomainValidationError("Every seed record must have provenance SYNTHETIC_DEMO.")
    return seed


def ensure_demo_source(session: Session) -> DataSource:
    src = session.scalar(select(DataSource).where(DataSource.namespace == DEMO_NAMESPACE))
    if src is None:
        src = DataSource(
            namespace=DEMO_NAMESPACE,
            name="ATLAS synthetic demo data",
            source_type=SourceType.SYNTHETIC_GENERATOR,
            provenance=Provenance.SYNTHETIC_DEMO,
            status=SourceStatus.ACTIVE,
            description="Deterministic synthetic demo dataset. Not real supplier data.",
        )
        session.add(src)
        session.flush()
    return src


def seed_demo_data(engine: Engine, settings: Settings, seed: SeedFile | None = None) -> SeedResult:
    if not settings.demo_data_allowed:
        raise ConflictError("Demo data is disabled by configuration (ATLAS_DEMO_DATA_ENABLED).")
    assert_schema_current(engine)  # fails clearly on an unmigrated DB; never creates tables
    seed = seed or load_seed_file(settings.demo_seed_path)
    result = SeedResult()

    with Session(engine, expire_on_commit=False) as session, session.begin():
        demo_source = ensure_demo_source(session)
        for item in seed.suppliers:
            existing = registry.get_supplier_by_code(session, item.supplier_code)
            if existing is None:
                registry.create_supplier(session, item, demo_source)
                result.created["suppliers"] += 1
            elif existing.provenance is Provenance.SYNTHETIC_DEMO:
                result.skipped_existing["suppliers"] += 1
            else:
                raise ConflictError(f"Code '{item.supplier_code}' belongs to a non-demo record; seed aborted.")
        for item in seed.facilities:
            existing = registry.get_facility_by_code(session, item.facility_code)
            if existing is None:
                registry.create_facility(session, item, demo_source)
                result.created["facilities"] += 1
            elif existing.provenance is Provenance.SYNTHETIC_DEMO:
                result.skipped_existing["facilities"] += 1
            else:
                raise ConflictError(f"Code '{item.facility_code}' belongs to a non-demo record; seed aborted.")
        for item in seed.relationships:
            existing_rel = session.scalar(
                select(SupplyRelationship).where(SupplyRelationship.relationship_code == item.relationship_code)
            )
            if existing_rel is None:
                registry.create_relationship(session, item, demo_source)
                result.created["relationships"] += 1
            elif existing_rel.provenance is Provenance.SYNTHETIC_DEMO:
                result.skipped_existing["relationships"] += 1
            else:
                raise ConflictError(f"Code '{item.relationship_code}' belongs to a non-demo record; seed aborted.")
        for pitem in seed.products:
            existing_p = session.scalar(select(Product).where(Product.product_code == pitem.product_code))
            if existing_p is None:
                registry.create_product(session, pitem, demo_source)
                result.created["products"] += 1
            elif existing_p.provenance is Provenance.SYNTHETIC_DEMO:
                result.skipped_existing["products"] += 1
            else:
                raise ConflictError(f"Code '{pitem.product_code}' belongs to a non-demo record; seed aborted.")
        for litem in seed.product_links:
            existing_l = session.scalar(select(ProductLink).where(ProductLink.link_code == litem.link_code))
            if existing_l is None:
                registry.create_product_link(session, litem, demo_source)
                result.created["product_links"] += 1
            elif existing_l.provenance is Provenance.SYNTHETIC_DEMO:
                result.skipped_existing["product_links"] += 1
            else:
                raise ConflictError(f"Code '{litem.link_code}' belongs to a non-demo record; seed aborted.")
        for item in seed.observations:
            existing_obs = session.scalar(
                select(Observation).where(Observation.observation_code == item.observation_code)
            )
            if existing_obs is None:
                registry.create_observation(session, item)
                result.created["observations"] += 1
            elif existing_obs.provenance is Provenance.SYNTHETIC_DEMO:
                result.skipped_existing["observations"] += 1
            else:
                raise ConflictError(f"Code '{item.observation_code}' belongs to a non-demo record; seed aborted.")
    logger.info(
        "demo_seed_completed", extra={"created_counts": result.created, "existing_counts": result.skipped_existing}
    )
    return result
