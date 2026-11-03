from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingestion import service
from app.ingestion.service import IngestRequest
from app.models.enums import EntityKind, IngestPolicy, SourceType
from app.models.product import Product
from app.models.supplier import Supplier

SUPPLIER_CSV = (
    "external_id,name,country_or_region,category,status\n"
    "S-1,Acme Parts,Germany,Electronics,ACTIVE\n"
    "S-2,Bolt Works,Mexico,Metal,ACTIVE\n"
    "S-3,Crate Co,Poland,Packaging,\n"
)


def new_source(session: Session, ns: str = "erp-a"):
    return service.create_source(
        session, namespace=ns, name=f"Source {ns}", source_type=SourceType.CSV_FILE, description=None
    )


def req(
    source_id: int,
    data: str | bytes,
    *,
    entity=EntityKind.SUPPLIER,
    fmt="CSV",
    policy=IngestPolicy.PARTIAL,
    profile_id=None,
    file_name="f.csv",
    encoding=None,
    delimiter=",",
) -> IngestRequest:
    raw = data.encode() if isinstance(data, str) else data
    return IngestRequest(source_id, entity, fmt, raw, profile_id, policy, encoding, delimiter, file_name)


def count(session: Session, model) -> int:
    session.rollback()
    return session.scalar(select(func.count()).select_from(model)) or 0


__all__ = ["SUPPLIER_CSV", "Product", "Supplier", "count", "new_source", "req"]
