"""Products and product-link read endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from app.api.v1.deps import VIEW, DbSession, Limit, Offset, SearchText
from app.core.errors import NotFoundError
from app.models.enums import EntityStatus, ProductLinkType, Provenance
from app.models.product import Product, ProductLink
from app.schemas.common import ErrorResponse, Page
from app.schemas.product import ProductDetail, ProductLinkRead, ProductRead
from app.services.mappers import product_link_read

router = APIRouter(tags=["products"])
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
OptId = Annotated[int | None, Query(ge=1, le=2**31 - 1)]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _links_stmt():
    return select(ProductLink).options(
        joinedload(ProductLink.product), joinedload(ProductLink.supplier), joinedload(ProductLink.facility)
    )


@router.get("/products", dependencies=VIEW, response_model=Page[ProductRead], summary="List products")
def list_products(
    session: DbSession,
    limit: Limit = 25,
    offset: Offset = 0,
    q: SearchText = None,
    status: EntityStatus | None = None,
    category: Annotated[str | None, Query(max_length=100)] = None,
    provenance: Provenance | None = None,
) -> Page[ProductRead]:
    stmt = select(Product)
    if q:
        stmt = stmt.where(
            or_(Product.name.ilike(_like(q), escape="\\"), Product.product_code.ilike(_like(q), escape="\\"))
        )
    if status:
        stmt = stmt.where(Product.status == status)
    if category:
        stmt = stmt.where(Product.category == category)
    if provenance:
        stmt = stmt.where(Product.provenance == provenance)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(Product.name, Product.id).limit(limit).offset(offset)).unique()
    return Page(items=[ProductRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get(
    "/products/{product_id}",
    dependencies=VIEW,
    response_model=ProductDetail,
    responses={404: {"model": ErrorResponse}},
    summary="Product with supplier/facility links",
)
def get_product(product_id: IdPath, session: DbSession) -> ProductDetail:
    p = session.get(Product, product_id)
    if p is None:
        raise NotFoundError(f"Product {product_id} was not found.")
    links = session.scalars(
        _links_stmt().where(ProductLink.product_id == product_id).order_by(ProductLink.id).limit(200)
    ).unique()
    return ProductDetail(
        **ProductRead.model_validate(p).model_dump(), links=[product_link_read(link) for link in links]
    )


@router.get("/product-links", dependencies=VIEW, response_model=Page[ProductLinkRead], summary="List product links")
def list_links(
    session: DbSession,
    limit: Limit = 50,
    offset: Offset = 0,
    product_id: OptId = None,
    supplier_id: OptId = None,
    facility_id: OptId = None,
    link_type: ProductLinkType | None = None,
) -> Page[ProductLinkRead]:
    stmt = _links_stmt()
    if product_id:
        stmt = stmt.where(ProductLink.product_id == product_id)
    if supplier_id:
        stmt = stmt.where(ProductLink.supplier_id == supplier_id)
    if facility_id:
        stmt = stmt.where(ProductLink.facility_id == facility_id)
    if link_type:
        stmt = stmt.where(ProductLink.link_type == link_type)
    total = (
        session.scalar(select(func.count()).select_from(select(ProductLink.id).where(*stmt._where_criteria).subquery()))
        or 0
    )
    rows = session.scalars(stmt.order_by(ProductLink.id).limit(limit).offset(offset)).unique()
    return Page(items=[product_link_read(r) for r in rows], total=total, limit=limit, offset=offset)
