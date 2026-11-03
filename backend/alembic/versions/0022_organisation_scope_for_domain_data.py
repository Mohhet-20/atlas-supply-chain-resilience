"""organisation scope for domain data: suppliers, facilities, products, network, inventory, runs and results

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-03

Every domain table gets ``organization_id`` (NOT NULL, default 1, foreign key to organizations, indexed); existing rows
belong to the default organisation, so nothing existing changes meaning. User-supplied business keys, idempotency keys
and import fingerprints become unique per organisation instead of globally, so one organisation can never detect another
organisation's records through a uniqueness conflict. quality_rule_settings is keyed by (organisation, rule).

On SQLite, tables whose constraints change are rebuilt (batch mode). Their standalone indexes and append-only
triggers are saved from sqlite_master first and restored afterwards, because a rebuild would otherwise drop them.
Every other table gets the column in place with ALTER TABLE ADD COLUMN, which keeps the table, its indexes and
triggers untouched.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    "candidate_reviews", "data_quality_findings", "data_sources", "decision_case_versions", "decision_cases",
    "decision_evaluations", "decision_events", "delivery_assessments", "delivery_events", "delivery_import_batches",
    "delivery_import_rows", "delivery_snapshots", "demand_observations", "duplicate_candidates", "entity_aliases",
    "entity_identifiers", "facilities", "forecast_alerts", "forecast_evaluations", "forecast_runs", "forecast_values",
    "graph_edge_events", "impact_alternatives", "impact_analyses", "impact_criticality", "impact_edge_snapshots",
    "impact_findings", "ingestion_batches", "ingestion_row_results", "inventory_cost_profiles",
    "inventory_import_batches", "inventory_import_rows", "inventory_policies", "inventory_positions",
    "inventory_replenishments", "leadtime_runs",
    "mapping_profiles", "merge_records", "model_diagnostics", "model_evaluations", "model_explanations",
    "model_lifecycle_events", "model_versions", "observations", "product_links", "products", "quality_assessments",
    "quality_eval_runs", "quality_finding_events", "quality_runs", "quality_scores", "record_lineage",
    "replenishment_decisions", "replenishment_runs", "resilience_decisions", "resilience_runs", "resolution_runs",
    "risk_alerts", "risk_assessments", "risk_evidence", "risk_factors", "risk_reviews", "risk_runs", "routing_runs",
    "scenario_events", "scenario_history", "scenarios", "simulation_alternatives", "simulation_edge_impacts",
    "simulation_impacts", "simulation_runs", "sourcing_decisions", "sourcing_offers", "sourcing_runs",
    "stockout_analyses", "stockout_findings", "suppliers", "supply_relationships", "transport_lanes",
)  # fmt: skip

# Column-level unique business keys: (table, old constraint, column). Replaced by uq_<table>_org_<column>.
COLUMN_KEYS = (
    ("suppliers", "uq_suppliers_supplier_code", "supplier_code"),
    ("facilities", "uq_facilities_facility_code", "facility_code"),
    ("products", "uq_products_product_code", "product_code"),
    ("product_links", "uq_product_links_link_code", "link_code"),
    ("supply_relationships", "uq_supply_relationships_relationship_code", "relationship_code"),
    ("observations", "uq_observations_observation_code", "observation_code"),
    ("data_sources", "uq_data_sources_namespace", "namespace"),
    ("forecast_alerts", "uq_forecast_alerts_fingerprint", "fingerprint"),
)

# Named unique constraints that keep their name and gain organization_id as the leading column.
NAMED_KEYS = (
    ("mapping_profiles", "uq_mapping_profiles_name_version", ["name", "version"]),
    ("risk_runs", "uq_risk_runs_idempotency_key", ["idempotency_key"]),
    ("simulation_runs", "uq_simulation_runs_idempotency_key", ["idempotency_key"]),
    ("impact_analyses", "uq_impact_analyses_idempotency_key", ["idempotency_key"]),
    ("inventory_import_batches", "uq_inv_import_batches_fingerprint", ["kind", "fingerprint"]),
    ("stockout_analyses", "uq_stockout_analyses_idempotency_key", ["idempotency_key"]),
    ("forecast_runs", "uq_forecast_runs_idempotency_key", ["idempotency_key"]),
    ("delivery_import_batches", "uq_delivery_import_batches_fingerprint", ["kind", "fingerprint"]),
    ("leadtime_runs", "uq_leadtime_runs_idempotency_key", ["idempotency_key"]),
    ("delivery_assessments", "uq_delivery_assessments_idempotency_key", ["idempotency_key"]),
    ("delivery_events", "uq_delivery_event_hash", ["row_hash"]),
    ("model_versions", "uq_model_versions_key_version", ["model_key", "version"]),
    ("model_evaluations", "uq_model_evaluations_idempotency_key", ["idempotency_key"]),
    ("routing_runs", "uq_routing_runs_idempotency_key", ["idempotency_key"]),
    ("sourcing_runs", "uq_sourcing_runs_idempotency_key", ["idempotency_key"]),
    ("replenishment_runs", "uq_replenishment_runs_idempotency_key", ["idempotency_key"]),
    ("resilience_runs", "uq_resilience_runs_idempotency_key", ["idempotency_key"]),
)

# Partial unique indexes (one live/applied row per key) that gain organization_id as the leading column.
PARTIAL_KEYS = {
    "uq_ingestion_batches_applied_fingerprint": ("ingestion_batches", ["fingerprint"]),
    "uq_data_quality_findings_eval_key": ("data_quality_findings", ["finding_key"]),
    "uq_quality_eval_runs_active_scope": ("quality_eval_runs", ["scope_key"]),
    "uq_risk_runs_active_scope": ("risk_runs", ["scope_key"]),
    "uq_risk_alerts_active_key": ("risk_alerts", ["alert_key"]),
}

FK = "fk_{t}_organization_id_organizations"
IX = "ix_{t}_organization_id"


def _rebuilt() -> set[str]:
    return {t for t, _, _ in COLUMN_KEYS} | {t for t, _, _ in NAMED_KEYS}


def _partial_sql(name: str, sql: str, *, scoped: bool) -> str:
    table, cols = PARTIAL_KEYS[name]
    plain, org = f"ON {table} ({', '.join(cols)})", f"ON {table} (organization_id, {', '.join(cols)})"
    old, new = (plain, org) if scoped else (org, plain)
    if old not in sql:
        raise RuntimeError(f"Unexpected definition of {name}: {sql}")
    return sql.replace(old, new)


def _saved_objects(table: str) -> list[tuple[str, str, str]]:
    """Standalone indexes and triggers of a table, as (type, name, sql). Constraint-backed indexes have no sql."""
    rows = op.get_bind().exec_driver_sql(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL",
        (table,),
    )
    return [(r[0], r[1], r[2]) for r in rows]


def _take_out(table: str, *, scoped: bool) -> list[str]:
    """Drop a table's standalone indexes and triggers before a rebuild; return the SQL that recreates them afterwards
    (partial unique keys adjusted to the target scope; the organisation index omitted on downgrade)."""
    bind = op.get_bind()
    objects = _saved_objects(table)
    for kind, name, _sql in objects:
        bind.exec_driver_sql(f"DROP {kind.upper()} IF EXISTS {name}")
    out = []
    for kind, name, sql in objects:
        if kind == "index" and name in PARTIAL_KEYS:
            sql = _partial_sql(name, sql, scoped=scoped)
        if kind == "index" and name == IX.format(t=table) and not scoped:
            continue
        out.append(sql)
    return out


def _sqlite_upgrade(table: str) -> None:
    bind = op.get_bind()
    if table not in _rebuilt():
        bind.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN organization_id INTEGER DEFAULT '1' NOT NULL "
            f"CONSTRAINT {FK.format(t=table)} REFERENCES organizations (id)"
        )
        for name, (t, _) in PARTIAL_KEYS.items():
            if t == table:
                sql = bind.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).scalar()
                bind.exec_driver_sql(f"DROP INDEX {name}")
                bind.exec_driver_sql(_partial_sql(name, sql, scoped=True))
        op.create_index(IX.format(t=table), table, ["organization_id"])
        return
    pending = _take_out(table, scoped=True)
    with op.batch_alter_table(table, recreate="always") as b:
        b.add_column(sa.Column("organization_id", sa.Integer(), server_default="1", nullable=False))
        b.create_foreign_key(FK.format(t=table), "organizations", ["organization_id"], ["id"])
        for t, old, col in COLUMN_KEYS:
            if t == table:
                b.drop_constraint(old, type_="unique")
                b.create_unique_constraint(f"uq_{t}_org_{col}", ["organization_id", col])
        for t, name, cols in NAMED_KEYS:
            if t == table:
                b.drop_constraint(name, type_="unique")
                b.create_unique_constraint(name, ["organization_id", *cols])
    for sql in pending:
        bind.exec_driver_sql(sql)
    op.create_index(IX.format(t=table), table, ["organization_id"])


def _sqlite_downgrade(table: str) -> None:
    bind = op.get_bind()
    pending = _take_out(table, scoped=False)
    with op.batch_alter_table(table, recreate="always") as b:
        for t, old, col in COLUMN_KEYS:
            if t == table:
                b.drop_constraint(f"uq_{t}_org_{col}", type_="unique")
                b.create_unique_constraint(old, [col])
        for t, name, cols in NAMED_KEYS:
            if t == table:
                b.drop_constraint(name, type_="unique")
                b.create_unique_constraint(name, cols)
        if table in _rebuilt():  # an in-place column's foreign key is unnamed and goes away with the column
            b.drop_constraint(FK.format(t=table), type_="foreignkey")
        b.drop_column("organization_id")
    for sql in pending:
        bind.exec_driver_sql(sql)


def _generic_upgrade(table: str) -> None:
    op.add_column(table, sa.Column("organization_id", sa.Integer(), server_default="1", nullable=False))
    op.create_foreign_key(FK.format(t=table), table, "organizations", ["organization_id"], ["id"])
    op.create_index(IX.format(t=table), table, ["organization_id"])
    for t, old, col in COLUMN_KEYS:
        if t == table:
            op.drop_constraint(old, table, type_="unique")
            op.create_unique_constraint(f"uq_{t}_org_{col}", table, ["organization_id", col])
    for t, name, cols in NAMED_KEYS:
        if t == table:
            op.drop_constraint(name, table, type_="unique")
            op.create_unique_constraint(name, table, ["organization_id", *cols])
    for name, (t, cols) in PARTIAL_KEYS.items():
        if t == table:
            where = sa.text(_PARTIAL_WHERE[name])
            op.drop_index(name, table_name=table)
            op.create_index(name, table, ["organization_id", *cols], unique=True, postgresql_where=where)


_PARTIAL_WHERE = {
    "uq_ingestion_batches_applied_fingerprint": "status IN ('COMMITTED','COMPLETED_WITH_ERRORS')",
    "uq_data_quality_findings_eval_key": "origin = 'EVALUATION'",
    "uq_quality_eval_runs_active_scope": "status IN ('QUEUED','RUNNING')",
    "uq_risk_runs_active_scope": "status IN ('QUEUED','RUNNING')",
    "uq_risk_alerts_active_key": "status IN ('OPEN','ACKNOWLEDGED')",
}


def _rule_settings(*, scoped: bool) -> None:
    """quality_rule_settings changes its primary key, which batch mode cannot do in place: copy into a new table."""
    org = [sa.Column("organization_id", sa.Integer(), server_default="1", nullable=False)] if scoped else []
    pk = ["organization_id", "rule_id"] if scoped else ["rule_id"]
    fk_name = FK.format(t="quality_rule_settings")
    # PostgreSQL names a primary key's index schema-wide, so the copy needs a temporary name until the old table is gone.
    sqlite = op.get_bind().dialect.name == "sqlite"
    pk_name = "pk_quality_rule_settings" if sqlite else "pk__quality_rule_settings_new"
    extra = [sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], name=fk_name)]
    op.create_table(
        "_quality_rule_settings_new",
        *org,
        sa.Column("rule_id", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        *(extra if scoped else []),
        sa.PrimaryKeyConstraint(*pk, name=pk_name),
    )
    if scoped:
        op.execute(
            "INSERT INTO _quality_rule_settings_new "
            "(organization_id, rule_id, enabled, reason, updated_by, updated_at, revision) "
            "SELECT 1, rule_id, enabled, reason, updated_by, updated_at, revision FROM quality_rule_settings"
        )
    else:  # the default organisation's overrides survive a downgrade; other organisations' cannot be represented
        op.execute(
            "INSERT INTO _quality_rule_settings_new (rule_id, enabled, reason, updated_by, updated_at, revision) "
            "SELECT rule_id, enabled, reason, updated_by, updated_at, revision FROM quality_rule_settings "
            "WHERE organization_id = 1"
        )
    op.drop_table("quality_rule_settings")
    op.rename_table("_quality_rule_settings_new", "quality_rule_settings")
    if not sqlite:
        op.execute(f"ALTER TABLE quality_rule_settings RENAME CONSTRAINT {pk_name} TO pk_quality_rule_settings")
    if scoped:
        op.create_index(IX.format(t="quality_rule_settings"), "quality_rule_settings", ["organization_id"])


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    for table in TABLES:
        (_sqlite_upgrade if sqlite else _generic_upgrade)(table)
    _rule_settings(scoped=True)


def downgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    op.drop_index(IX.format(t="quality_rule_settings"), table_name="quality_rule_settings")
    _rule_settings(scoped=False)
    for table in reversed(TABLES):
        if sqlite:
            _sqlite_downgrade(table)
            continue
        for name, (t, cols) in PARTIAL_KEYS.items():
            if t == table:
                op.drop_index(name, table_name=table)
                op.create_index(name, table, cols, unique=True, postgresql_where=sa.text(_PARTIAL_WHERE[name]))
        for t, name, cols in NAMED_KEYS:
            if t == table:
                op.drop_constraint(name, table, type_="unique")
                op.create_unique_constraint(name, table, cols)
        for t, old, col in COLUMN_KEYS:
            if t == table:
                op.drop_constraint(f"uq_{t}_org_{col}", table, type_="unique")
                op.create_unique_constraint(old, table, [col])
        op.drop_index(IX.format(t=table), table_name=table)
        op.drop_constraint(FK.format(t=table), table, type_="foreignkey")
        op.drop_column(table, "organization_id")
