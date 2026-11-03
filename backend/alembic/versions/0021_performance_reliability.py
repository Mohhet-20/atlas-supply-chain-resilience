"""performance and reliability: alerts list index, webhook delivery lease, integration circuit breaker

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-02

Additive and reversible. New columns are nullable or carry a server default, so existing rows stay valid. The index serves
the tenant-scoped "newest alerts" listing (removes a temporary B-tree sort; see docs/architecture.md).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_alerts_org_last_seen", "alerts", ["organization_id", "last_seen_at"])
    with op.batch_alter_table("webhook_deliveries") as b:
        b.add_column(sa.Column("lease_until", sa.DateTime(), nullable=True))
    with op.batch_alter_table("integrations") as b:
        b.add_column(sa.Column("consecutive_failed_runs", sa.Integer(), server_default="0", nullable=False))
        b.add_column(sa.Column("circuit_open_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("integrations") as b:
        b.drop_column("circuit_open_until")
        b.drop_column("consecutive_failed_runs")
    with op.batch_alter_table("webhook_deliveries") as b:
        b.drop_column("lease_until")
    op.drop_index("ix_alerts_org_last_seen", table_name="alerts")
