"""network graph: relationship status, temporal and quality attributes, history

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22 06:40:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUS = ('PROPOSED', 'CONFIRMED', 'REJECTED', 'INACTIVE')
CRIT = ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')


def _enum(values, name):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def _refuse_if_duplicates() -> None:
    """The new pair indexes forbid duplicate relationships. Never rewrite data to satisfy them: stop and say which."""
    bind = op.get_bind()
    dups = bind.execute(sa.text(
        "SELECT relationship_type, supplier_id, source_facility_id, target_facility_id, COUNT(*) c "
        "FROM supply_relationships GROUP BY relationship_type, supplier_id, source_facility_id, target_facility_id "
        "HAVING COUNT(*) > 1"
    )).fetchall()
    if dups:
        listing = "; ".join(f"{r[0]} supplier={r[1]} source_facility={r[2]} target_facility={r[3]} x{r[4]}" for r in dups[:10])
        raise RuntimeError(
            "Migration 0004 cannot add the unique relationship identity index: duplicate supply_relationships exist "
            f"({listing}). Resolve them (for example with the registry merge workflow) and re-run. No data was changed."
        )


def upgrade() -> None:
    _refuse_if_duplicates()
    op.create_table('graph_edge_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('edge_kind', sa.String(length=16), nullable=False),
        sa.Column('edge_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('from_status', sa.String(length=32), nullable=True),
        sa.Column('to_status', sa.String(length=32), nullable=True),
        sa.Column('actor', sa.String(length=64), nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=True),
        sa.Column('changes', sa.JSON(), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.CheckConstraint("edge_kind IN ('SUPPLY','PRODUCT')", name=op.f('ck_graph_edge_events_edge_kind')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_graph_edge_events')),
    )
    with op.batch_alter_table('graph_edge_events') as b:
        b.create_index('ix_graph_edge_events_edge', ['edge_kind', 'edge_id'], unique=False)
        b.create_index('ix_graph_edge_events_occurred', ['occurred_at'], unique=False)

    with op.batch_alter_table('product_links') as b:
        # Existing rows were asserted by seed data or an ingestion batch: they become CONFIRMED; confidence stays unknown.
        b.add_column(sa.Column('status', _enum(STATUS, 'product_link_status'), server_default='CONFIRMED', nullable=False))
        b.add_column(sa.Column('status_reason', sa.String(length=500), nullable=True))
        b.add_column(sa.Column('confidence', sa.Float(), nullable=True))
        b.add_column(sa.Column('criticality', _enum(CRIT, 'product_link_criticality'), nullable=True))
        b.add_column(sa.Column('supply_share_pct', sa.Float(), nullable=True))
        b.add_column(sa.Column('created_by', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('updated_by', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('version', sa.Integer(), server_default='1', nullable=False))
        b.create_check_constraint('confidence_range', 'confidence IS NULL OR (confidence >= 0 AND confidence <= 1)')
        b.create_check_constraint('share_range', 'supply_share_pct IS NULL OR (supply_share_pct >= 0 AND supply_share_pct <= 100)')
        b.create_index('ix_product_links_status', ['status'], unique=False)

    with op.batch_alter_table('supply_relationships') as b:
        b.add_column(sa.Column('status', _enum(STATUS, 'supply_relationship_status'), server_default='CONFIRMED', nullable=False))
        b.add_column(sa.Column('status_reason', sa.String(length=500), nullable=True))
        b.add_column(sa.Column('effective_from', sa.Date(), nullable=True))
        b.add_column(sa.Column('effective_to', sa.Date(), nullable=True))
        b.add_column(sa.Column('confidence', sa.Float(), nullable=True))
        b.add_column(sa.Column('criticality', _enum(CRIT, 'supply_relationship_criticality'), nullable=True))
        b.add_column(sa.Column('supply_share_pct', sa.Float(), nullable=True))
        b.add_column(sa.Column('created_by', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('updated_by', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('version', sa.Integer(), server_default='1', nullable=False))
        b.add_column(sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False))
        b.create_check_constraint('confidence_range', 'confidence IS NULL OR (confidence >= 0 AND confidence <= 1)')
        b.create_check_constraint('share_range', 'supply_share_pct IS NULL OR (supply_share_pct >= 0 AND supply_share_pct <= 100)')
        b.create_check_constraint('effective_range', 'effective_from IS NULL OR effective_to IS NULL OR effective_to >= effective_from')
        b.create_index('ix_supply_relationships_status', ['status'], unique=False)
        b.create_index('uq_supply_relationships_supplier_pair', ['relationship_type', 'supplier_id', 'target_facility_id'], unique=True,
                       sqlite_where=sa.text('supplier_id IS NOT NULL'), postgresql_where=sa.text('supplier_id IS NOT NULL'))
        b.create_index('uq_supply_relationships_facility_pair', ['relationship_type', 'source_facility_id', 'target_facility_id'], unique=True,
                       sqlite_where=sa.text('source_facility_id IS NOT NULL'), postgresql_where=sa.text('source_facility_id IS NOT NULL'))
    op.execute("UPDATE supply_relationships SET updated_at = created_at")

    if op.get_bind().dialect.name == "sqlite":
        for verb in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_graph_edge_events_no_{verb.lower()} BEFORE {verb} ON graph_edge_events "
                "BEGIN SELECT RAISE(ABORT, 'graph_edge_events is append-only'); END"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for verb in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_graph_edge_events_no_{verb}")
    with op.batch_alter_table('supply_relationships') as b:
        b.drop_index('uq_supply_relationships_facility_pair')
        b.drop_index('uq_supply_relationships_supplier_pair')
        b.drop_index('ix_supply_relationships_status')
        b.drop_constraint(b.f('ck_supply_relationships_effective_range'), type_='check')
        b.drop_constraint(b.f('ck_supply_relationships_share_range'), type_='check')
        b.drop_constraint(b.f('ck_supply_relationships_confidence_range'), type_='check')
        b.drop_constraint(b.f('ck_supply_relationships_supply_relationship_status'), type_='check')
        b.drop_constraint(b.f('ck_supply_relationships_supply_relationship_criticality'), type_='check')
        for col in ('updated_at', 'version', 'updated_by', 'created_by', 'supply_share_pct', 'criticality', 'confidence',
                    'effective_to', 'effective_from', 'status_reason', 'status'):
            b.drop_column(col)
    with op.batch_alter_table('product_links') as b:
        b.drop_index('ix_product_links_status')
        b.drop_constraint(b.f('ck_product_links_share_range'), type_='check')
        b.drop_constraint(b.f('ck_product_links_confidence_range'), type_='check')
        b.drop_constraint(b.f('ck_product_links_product_link_status'), type_='check')
        b.drop_constraint(b.f('ck_product_links_product_link_criticality'), type_='check')
        for col in ('version', 'updated_by', 'created_by', 'supply_share_pct', 'criticality', 'confidence',
                    'status_reason', 'status'):
            b.drop_column(col)
    with op.batch_alter_table('graph_edge_events') as b:
        b.drop_index('ix_graph_edge_events_occurred')
        b.drop_index('ix_graph_edge_events_edge')
    op.drop_table('graph_edge_events')
