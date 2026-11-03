"""append-only guards on PostgreSQL: the audit log and review/history tables refuse UPDATE and DELETE

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-03

SQLite has had these guards since the tables were created (triggers that RAISE(ABORT)). PostgreSQL had none, so on
PostgreSQL the hash-chained audit log and the review histories could be rewritten with plain SQL. This adds the same
guard there: one trigger function and a BEFORE UPDATE / BEFORE DELETE trigger per table, with the SQLite trigger names.
On SQLite this revision does nothing.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APPEND_ONLY = (
    "audit_events", "candidate_reviews", "graph_edge_events", "quality_finding_events", "risk_reviews",
    "scenario_history",
)  # fmt: skip


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE OR REPLACE FUNCTION atlas_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation'; END $$"
    )
    for table in APPEND_ONLY:
        for verb in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION atlas_append_only()"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in APPEND_ONLY:
        for verb in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_{verb} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS atlas_append_only()")
