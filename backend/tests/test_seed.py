import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import ConflictError, DomainValidationError, SchemaNotMigratedError
from app.db.session import create_db_engine
from app.models import Facility, Observation, Supplier, SupplyRelationship
from app.models.enums import Provenance
from app.schemas.supplier import SupplierCreate
from app.services import registry
from app.services.seed import load_seed_file, seed_demo_data

MODELS = (Supplier, Facility, SupplyRelationship, Observation)


def counts(session):
    return [session.scalar(select(func.count()).select_from(m)) for m in MODELS]


def test_seed_creates_deterministic_synthetic_records(seeded, session):
    assert seeded.created == {
        "suppliers": 7,
        "facilities": 5,
        "relationships": 11,
        "products": 4,
        "product_links": 8,
        "observations": 12,
    }
    assert counts(session) == [7, 5, 11, 12]
    for model in MODELS:
        provs = set(session.scalars(select(model.provenance)))
        assert provs == {Provenance.SYNTHETIC_DEMO}
    codes = set(session.scalars(select(Supplier.supplier_code)))
    assert codes == {f"DEMO-SUP-00{i}" for i in range(1, 8)}


def test_seeding_twice_creates_no_duplicates(settings, engine, session, seeded):
    again = seed_demo_data(engine, settings)
    assert again.created == dict.fromkeys(
        ("suppliers", "facilities", "relationships", "products", "product_links", "observations"), 0
    )
    assert again.skipped_existing == {
        "suppliers": 7,
        "facilities": 5,
        "relationships": 11,
        "products": 4,
        "product_links": 8,
        "observations": 12,
    }
    assert counts(session) == [7, 5, 11, 12]


def test_seed_refuses_unmigrated_database_and_creates_no_tables(settings, tmp_path):
    from sqlalchemy import inspect

    bare = create_db_engine(f"sqlite:///{(tmp_path / 'bare.db').as_posix()}")
    with pytest.raises(SchemaNotMigratedError, match="alembic upgrade head"):
        seed_demo_data(bare, settings)
    assert inspect(bare).get_table_names() == []
    bare.dispose()


def test_seed_does_not_delete_or_overwrite_user_records(settings, engine, session):
    with session.begin():
        registry.create_supplier(
            session,
            SupplierCreate(
                supplier_code="USER-1",
                name="My Supplier",
                country_or_region="X",
                category="C",
                provenance="USER_PROVIDED",
            ),
        )
    seed_demo_data(engine, settings)
    seed_demo_data(engine, settings)
    session.expire_all()
    mine = session.scalar(select(Supplier).where(Supplier.supplier_code == "USER-1"))
    assert mine is not None and mine.name == "My Supplier" and mine.provenance is Provenance.USER_PROVIDED
    assert session.scalar(select(func.count()).select_from(Supplier)) == 8


def test_collision_with_user_record_aborts_and_rolls_back_everything(settings, engine, session):
    with session.begin():
        registry.create_supplier(
            session,
            SupplierCreate(
                supplier_code="DEMO-SUP-003",
                name="User owned",
                country_or_region="X",
                category="C",
                provenance="USER_PROVIDED",
            ),
        )
    with pytest.raises(ConflictError):
        seed_demo_data(engine, settings)
    session.expire_all()
    assert counts(session) == [1, 0, 0, 0]  # nothing partially committed
    assert session.scalar(select(Supplier.name)) == "User owned"


def test_mid_seed_failure_rolls_back_transaction(settings, engine, session, tmp_path):
    doc = json.loads(settings.demo_seed_path.read_text())
    doc["relationships"][-1]["target_facility_code"] = "DEMO-FAC-999"  # unknown reference
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(DomainValidationError):
        seed_demo_data(engine, settings, load_seed_file(path))
    assert counts(session) == [0, 0, 0, 0]


def test_seed_file_must_be_entirely_synthetic(settings, tmp_path):
    doc = json.loads(settings.demo_seed_path.read_text())
    doc["suppliers"][0]["provenance"] = "USER_PROVIDED"
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(DomainValidationError, match="SYNTHETIC_DEMO"):
        load_seed_file(path)


def test_invalid_or_missing_seed_file(tmp_path):
    with pytest.raises(DomainValidationError):
        load_seed_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(DomainValidationError):
        load_seed_file(bad)


def test_seed_disabled_by_configuration(engine, tmp_path, db_url):
    s = Settings(
        _env_file=None,
        environment="production",
        cors_allowed_origins=["https://x.example"],
        database_url=db_url,
        data_dir=tmp_path,
    )
    with pytest.raises(ConflictError):
        seed_demo_data(engine, s)


def test_seed_file_content_is_clearly_fictional():
    seed = load_seed_file(Path(__file__).resolve().parents[2] / "data" / "demo" / "demo_seed.json")
    assert all(s.name.startswith("Synthetic ") for s in seed.suppliers)
    assert "FICTIONAL" in seed.notice
    assert any(o.observed_at.utcoffset().total_seconds() != 0 for o in seed.observations)  # offsets exercised
