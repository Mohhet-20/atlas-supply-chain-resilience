"""Migration 0008 -> 0009, insert-only records, constraints."""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.migrations_check import alembic_config
from app.db.session import create_db_engine
from app.models.enums import Provenance
from app.models.inventory import (
    DemandKind,
    DemandObservation,
    ImportKind,
    ImportStatus,
    InventoryImportBatch,
    InventoryPolicy,
    InventoryPosition,
    PolicyStatus,
)
from app.models.types import utcnow
from tests.inventory_helpers import code, iso, load_inventory, world

TABLES = {
    "inventory_import_batches", "inventory_import_rows", "inventory_positions", "demand_observations",
    "inventory_replenishments", "inventory_policies", "stockout_analyses", "stockout_findings",
}  # fmt: skip


def test_upgrade_from_0008_keeps_data_and_adds_empty_tables(tmp_path: pathlib.Path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    conf = alembic_config(url)
    command.upgrade(conf, "0008")
    eng = create_db_engine(url)
    assert TABLES.isdisjoint(inspect(eng).get_table_names())
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO suppliers (supplier_code, name, name_normalized, country_or_region, category, status, provenance, version, created_at, updated_at) VALUES ('S-1','Legacy Supplier','legacy supplier','Germany','Parts','ACTIVE','USER_PROVIDED',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
    eng.dispose()
    command.upgrade(conf, "head")
    eng = create_db_engine(url)
    assert TABLES <= set(inspect(eng).get_table_names())
    with eng.connect() as c:
        assert c.execute(text("SELECT name FROM suppliers")).scalar() == "Legacy Supplier"
        for t in TABLES:
            assert c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() == 0  # noqa: S608 - nothing is seeded or invented
    assert "uq_inv_policy_active_pair" in {i["name"] for i in inspect(eng).get_indexes("inventory_policies")}
    eng.dispose()
    command.downgrade(conf, "0008")
    eng = create_db_engine(url)
    assert TABLES.isdisjoint(inspect(eng).get_table_names())
    with eng.connect() as c:
        assert c.execute(text("SELECT name FROM suppliers")).scalar() == "Legacy Supplier"
    eng.dispose()


def _pol(pid, fid, version, status=PolicyStatus.ACTIVE):
    return InventoryPolicy(
        product_id=pid, facility_id=fid, version=version, status=status, unit="EA", safety_stock=1.0, effective_from=utcnow(),
        owner="o", provenance=Provenance.USER_PROVIDED, created_by="t", created_at=utcnow(),
    )  # fmt: skip


def test_positions_are_insert_only_and_unique(client):
    w = world(client)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(1), "EA", 10, 0, "", "", "", "", ""]])
    with Session(client.app.state.engine) as s:
        p = s.query(InventoryPosition).one()
        p.on_hand = 999
        with pytest.raises(Exception, match="(?i)insert-only|immutable|cannot be"):
            s.commit()
        s.rollback()
        p = s.query(InventoryPosition).one()
        s.delete(p)
        with pytest.raises(Exception, match="(?i)insert-only|immutable|cannot be"):
            s.commit()
        s.rollback()
        dup = InventoryPosition(
            product_id=p.product_id, facility_id=p.facility_id, batch_id=p.batch_id, row_hash="x", observed_at=p.observed_at, unit="EA",
            on_hand=1.0, provenance=Provenance.USER_PROVIDED, source_label="x",
        )  # fmt: skip
        s.add(dup)
        with pytest.raises(IntegrityError):
            s.commit()


def test_database_rejects_negative_quantities(client):
    w = world(client)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(1), "EA", 10, 0, "", "", "", "", ""]])
    with Session(client.app.state.engine) as s:
        p = s.query(InventoryPosition).one()
        b = s.get(InventoryImportBatch, p.batch_id)
        assert b.kind is ImportKind.INVENTORY and b.status is ImportStatus.COMPLETED
        s.add(
            InventoryPosition(
                product_id=p.product_id,
                facility_id=p.facility_id,
                batch_id=b.id,
                row_hash="y",
                observed_at=utcnow(),
                unit="EA",
                on_hand=-1.0,
                provenance=Provenance.USER_PROVIDED,
                source_label="x",
            )  # fmt: skip
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_one_active_policy_per_pair(client):
    w = world(client)
    pid, fid = w["p1"]["id"], w["f1"]["id"]
    with Session(client.app.state.engine) as s:
        s.add(_pol(pid, fid, 1))
        s.commit()
        s.add(_pol(pid, fid, 2))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(_pol(pid, fid, 2, PolicyStatus.SUPERSEDED))
        s.commit()
        s.add(_pol(pid, fid, 2, PolicyStatus.SUPERSEDED))
        with pytest.raises(IntegrityError):
            s.commit()


def test_demand_period_must_be_ordered(client):
    w = world(client)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(1), "EA", 10, 0, "", "", "", "", ""]])
    from datetime import date

    with Session(client.app.state.engine) as s:
        b = s.query(InventoryImportBatch).one()
        s.add(
            DemandObservation(
                product_id=w["p1"]["id"],
                facility_id=w["f1"]["id"],
                batch_id=b.id,
                kind=DemandKind.DEMAND,
                period_start=date(2026, 1, 5),
                period_end=date(2026, 1, 5),
                quantity=1.0,
                unit="EA",
                row_hash="z",
                provenance=Provenance.USER_PROVIDED,
                source_label="x",
            )  # fmt: skip
        )
        with pytest.raises(IntegrityError):
            s.commit()
