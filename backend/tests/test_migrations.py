import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

import app.models  # noqa: F401
from alembic import command
from app.db.base import Base
from app.db.migrations_check import alembic_config, assert_schema_current, current_revision, head_revision

pytestmark = pytest.mark.sqlite_only  # SQLite files, PRAGMAs and DDL; see conftest


def test_head_revision_is_recorded_and_applied(engine):
    assert head_revision() == "0023"
    assert current_revision(engine) == "0023"
    assert assert_schema_current(engine) == "0023"


def test_models_match_migrations_no_drift(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_expected_tables_and_indexes_exist(engine):
    insp = inspect(engine)
    assert {"suppliers", "facilities", "supply_relationships", "observations", "alembic_version"} <= set(
        insp.get_table_names()
    )
    names = {ix["name"] for ix in insp.get_indexes("observations")}
    assert {"ix_observations_observed_at", "ix_observations_metric_name"} <= names


def test_fresh_database_upgrade_and_downgrade(tmp_path):
    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    assert "suppliers" in inspect(eng).get_table_names()
    command.downgrade(cfg, "base")
    assert "suppliers" not in inspect(eng).get_table_names()
    eng.dispose()


def test_phase13_migration_is_reversible_and_keeps_earlier_tables(tmp_path):
    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p13.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0013")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    assert {"transport_lanes", "routing_runs", "model_versions"} <= tables
    idx = {ix["name"] for ix in inspect(eng).get_indexes("transport_lanes")}
    assert {"ix_transport_lanes_origin", "ix_transport_lanes_destination", "ix_transport_lanes_relationship"} <= idx
    command.downgrade(cfg, "0012")
    after = set(inspect(eng).get_table_names())
    assert "transport_lanes" not in after and "routing_runs" not in after and "model_versions" in after
    command.upgrade(cfg, "head")
    assert "routing_runs" in set(inspect(eng).get_table_names())
    eng.dispose()


def test_phase14_migration_is_reversible_and_keeps_earlier_tables(tmp_path):
    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p14.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0014")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    assert {"sourcing_offers", "sourcing_runs", "sourcing_decisions", "transport_lanes"} <= tables
    idx = {ix["name"] for ix in inspect(eng).get_indexes("sourcing_offers")}
    assert {"ix_sourcing_offers_pair", "ix_sourcing_offers_supplier"} <= idx
    command.downgrade(cfg, "0013")
    after = set(inspect(eng).get_table_names())
    assert "sourcing_offers" not in after and "sourcing_runs" not in after and "transport_lanes" in after
    command.upgrade(cfg, "head")
    assert "sourcing_decisions" in set(inspect(eng).get_table_names())
    eng.dispose()


def test_phase15_migration_is_reversible_and_keeps_earlier_tables(tmp_path):
    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p15.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0015")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    assert {
        "inventory_cost_profiles",
        "replenishment_runs",
        "replenishment_decisions",
        "sourcing_offers",
        "inventory_policies",
    } <= tables
    command.downgrade(cfg, "0014")
    after = set(inspect(eng).get_table_names())
    assert "replenishment_runs" not in after and "inventory_cost_profiles" not in after and "sourcing_offers" in after
    command.upgrade(cfg, "head")
    assert "replenishment_decisions" in set(inspect(eng).get_table_names())
    eng.dispose()


def test_phase16_migration_is_reversible_and_keeps_earlier_tables(tmp_path):
    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p16.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0016")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    assert {"resilience_runs", "resilience_decisions", "replenishment_runs", "sourcing_offers"} <= tables
    command.downgrade(cfg, "0015")
    after = set(inspect(eng).get_table_names())
    assert "resilience_runs" not in after and "resilience_decisions" not in after and "replenishment_runs" in after
    command.upgrade(cfg, "head")
    assert "resilience_decisions" in set(inspect(eng).get_table_names())
    eng.dispose()


def test_phase17_migration_is_reversible_and_keeps_earlier_tables(tmp_path):
    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p17.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0017")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    assert {
        "decision_cases",
        "decision_case_versions",
        "decision_evaluations",
        "decision_events",
        "resilience_runs",
    } <= tables
    command.downgrade(cfg, "0016")
    after = set(inspect(eng).get_table_names())
    assert not any(t.startswith("decision_") for t in after) and "resilience_runs" in after
    command.upgrade(cfg, "head")
    assert "decision_events" in set(inspect(eng).get_table_names())
    eng.dispose()


def test_phase19_migration_is_reversible_and_keeps_earlier_tables(tmp_path):
    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p19.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0018")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    assert {"alerts", "alert_events", "alert_rules", "notification_deliveries", "inbox_notifications"} <= tables
    command.downgrade(cfg, "0017")
    after = set(inspect(eng).get_table_names())
    assert "alerts" not in after and "decision_cases" in after
    command.upgrade(cfg, "head")
    assert "alerts" in set(inspect(eng).get_table_names())
    eng.dispose()


def test_phase20_migration_is_reversible_and_keeps_alerting_data(tmp_path):
    from sqlalchemy import text

    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p20.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0018")
    eng = create_db_engine(url)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO alert_escalation_policies (policy_uid, name, enabled, ack_minutes, escalate_after_minutes, targets, version, created_by, created_at, updated_at) VALUES ('EP-1','Std',1,10,10,'[]',1,'x','2026-01-01','2026-01-01')"  # noqa: E501
            )
        )
    command.upgrade(cfg, "0019")
    insp = inspect(eng)
    assert {"organizations", "users", "auth_sessions", "password_reset_tokens", "login_throttles"} <= set(
        insp.get_table_names()
    )
    with eng.connect() as c:
        assert (
            c.execute(text("SELECT organization_id FROM alert_escalation_policies")).scalar() == 1
        )  # existing rows join the default org
        assert c.execute(text("SELECT slug FROM organizations WHERE id = 1")).scalar() == "default"
        assert (
            c.execute(
                text("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_events'")
            ).scalar()
            == 2
        )
    command.downgrade(cfg, "0018")
    assert "users" not in set(inspect(eng).get_table_names())
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM alert_escalation_policies")).scalar() == 1
        assert (
            c.execute(
                text("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_events'")
            ).scalar()
            == 2
        )
    command.upgrade(cfg, "head")
    eng.dispose()


def test_phase21_migration_is_additive_and_reversible(tmp_path):
    from sqlalchemy import text

    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p21.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0019")
    eng = create_db_engine(url)
    before = set(inspect(eng).get_table_names())
    command.upgrade(cfg, "0020")
    new = set(inspect(eng).get_table_names()) - before
    assert new == {
        "service_accounts", "api_keys", "integrations", "integration_runs", "sync_checkpoints", "sync_errors",
        "webhook_endpoints", "webhook_deliveries", "inbound_events", "export_jobs",
    }  # fmt: skip
    with eng.connect() as c:  # a second active run for one integration is refused by the database itself
        idx = {r[1] for r in c.execute(text("PRAGMA index_list(integration_runs)"))}
        assert "uq_integration_runs_active" in idx
    command.downgrade(cfg, "0019")
    assert set(inspect(eng).get_table_names()) == before  # nothing else was touched
    command.upgrade(cfg, "head")
    eng.dispose()


def test_phase22_migration_is_additive_reversible_and_preserves_data(tmp_path):
    from sqlalchemy import text

    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p22.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0020")
    eng = create_db_engine(url)
    tables = set(inspect(eng).get_table_names())
    with eng.begin() as c:  # an existing integration row must survive and receive the column defaults
        c.execute(
            text(
                "INSERT INTO integrations (integration_uid, organization_id, name, category, provider, config, mapping, target, status, enabled, version, created_at, updated_at) "
                "VALUES ('INT-1', 1, 'n', 'inventory', 'sandbox_inventory', '{}', '{}', 'alert_events', 'draft', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(cfg, "0021")
    cols = {c["name"] for c in inspect(eng).get_columns("integrations")}
    assert {"consecutive_failed_runs", "circuit_open_until"} <= cols
    assert "lease_until" in {c["name"] for c in inspect(eng).get_columns("webhook_deliveries")}
    assert "ix_alerts_org_last_seen" in {i["name"] for i in inspect(eng).get_indexes("alerts")}
    with eng.connect() as c:
        assert (
            c.execute(text("SELECT consecutive_failed_runs FROM integrations WHERE integration_uid='INT-1'")).scalar()
            == 0
        )
        plan = " ".join(
            str(r)
            for r in c.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT * FROM alerts WHERE organization_id=1 ORDER BY last_seen_at DESC LIMIT 50"
                )
            )
        )
        assert "ix_alerts_org_last_seen" in plan and "TEMP B-TREE" not in plan
    command.downgrade(cfg, "0020")
    assert set(inspect(eng).get_table_names()) == tables
    assert "lease_until" not in {c["name"] for c in inspect(eng).get_columns("webhook_deliveries")}
    command.upgrade(cfg, "head")
    eng.dispose()


def test_organisation_scope_migration_keeps_populated_data_triggers_and_is_reversible(tmp_path):
    """0021 -> 0022 on a database with parent and child rows: rebuilt tables keep their rows and children, rows land in
    the default organisation, append-only triggers survive, per-organisation keys apply, and the downgrade restores it."""
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError, IntegrityError

    from app.db.session import create_db_engine

    url = f"sqlite:///{(tmp_path / 'p0022.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0021")
    eng = create_db_engine(url)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO suppliers (id, supplier_code, name, country_or_region, category, status, provenance, created_at, updated_at) "
                "VALUES (1, 'SUP-1', 'Synthetic Metals', 'DE', 'Metals', 'ACTIVE', 'SYNTHETIC_DEMO', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        c.execute(
            text(
                "INSERT INTO facilities (id, facility_code, name, facility_type, country_or_region, status, provenance, created_at, updated_at) "
                "VALUES (1, 'FAC-1', 'Synthetic Plant', 'MANUFACTURING', 'DE', 'ACTIVE', 'SYNTHETIC_DEMO', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        c.execute(
            text(
                "INSERT INTO supply_relationships (relationship_code, relationship_type, supplier_id, target_facility_id, provenance, created_at) "
                "VALUES ('REL-1', 'SUPPLIES', 1, 1, 'SYNTHETIC_DEMO', CURRENT_TIMESTAMP)"
            )
        )
        c.execute(
            text(
                "INSERT INTO graph_edge_events (occurred_at, edge_kind, edge_id, action, actor, changes) "
                "VALUES (CURRENT_TIMESTAMP, 'SUPPLY', 1, 'CREATED', 'migration-test', '{}')"
            )
        )
        c.execute(
            text(
                "INSERT INTO quality_rule_settings (rule_id, enabled, reason, updated_by, updated_at, revision) "
                "VALUES ('DQ001', 0, 'synthetic', 'test', CURRENT_TIMESTAMP, 1)"
            )
        )
    command.upgrade(cfg, "0022")
    with eng.begin() as c:
        assert c.execute(text("SELECT organization_id FROM suppliers")).scalar() == 1
        assert c.execute(text("SELECT supplier_id FROM supply_relationships")).scalar() == 1
        assert c.execute(text("SELECT organization_id, rule_id FROM quality_rule_settings")).one() == (1, "DQ001")
        c.execute(text("INSERT INTO organizations (id, org_uid, slug, name, status, created_at) "
                       "VALUES (2, 'OR-2', 'second', 'Second', 'active', CURRENT_TIMESTAMP)"))  # fmt: skip
        c.execute(
            text(  # the same business code is allowed in another organisation
                "INSERT INTO suppliers (supplier_code, name, country_or_region, category, status, provenance, created_at, "
                "updated_at, organization_id) VALUES ('SUP-1', 'Other', 'DE', 'Metals', 'ACTIVE', 'SYNTHETIC_DEMO', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 2)"
            )
        )
    with pytest.raises(IntegrityError), eng.begin() as c:  # but not twice in one organisation
        c.execute(
            text(
                "INSERT INTO suppliers (supplier_code, name, country_or_region, category, status, provenance, created_at, "
                "updated_at) VALUES ('SUP-1', 'Dup', 'DE', 'Metals', 'ACTIVE', 'SYNTHETIC_DEMO', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    with pytest.raises(DatabaseError, match="append-only"), eng.begin() as c:
        c.execute(text("UPDATE graph_edge_events SET actor = 'x'"))
    with pytest.raises(IntegrityError), eng.begin() as c:  # foreign keys still enforced after the rebuild
        c.execute(text("UPDATE supply_relationships SET supplier_id = 999"))
    with eng.begin() as c:
        c.execute(text("DELETE FROM suppliers WHERE organization_id = 2"))
        c.execute(text("DELETE FROM organizations WHERE id = 2"))
    command.downgrade(cfg, "0021")
    with eng.connect() as c:
        assert "organization_id" not in {col["name"] for col in inspect(eng).get_columns("suppliers")}
        assert c.execute(text("SELECT supplier_id FROM supply_relationships")).scalar() == 1
        assert c.execute(text("SELECT rule_id FROM quality_rule_settings")).scalar() == "DQ001"
    with pytest.raises(DatabaseError, match="append-only"), eng.begin() as c:
        c.execute(text("DELETE FROM graph_edge_events"))
    eng.dispose()
