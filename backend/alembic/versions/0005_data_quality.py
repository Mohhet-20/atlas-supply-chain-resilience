"""data quality: evaluation runs, scores, finding lifecycle and history, rule settings

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_RESOLUTION = ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'WONT_FIX')
NEW_RESOLUTION = ('OPEN', 'ACKNOWLEDGED', 'IN_REMEDIATION', 'RESOLVED', 'ACCEPTED_EXCEPTION', 'REOPENED', 'WONT_FIX')
SCOPES = ('ENTITY', 'RELATIONSHIPS', 'BATCH', 'SOURCE', 'NETWORK')
STATUSES = ('QUEUED', 'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')
KINDS = ('SUPPLIER', 'FACILITY', 'PRODUCT', 'RELATIONSHIP', 'PRODUCT_LINK')
ORIGINS = ('INGESTION', 'EVALUATION')


def _enum(values, name):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def _in(values):
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        'quality_eval_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_uid', sa.String(length=40), nullable=False),
        sa.Column('scope_type', _enum(SCOPES, 'eval_scope'), nullable=False),
        sa.Column('scope_ref', sa.JSON(), nullable=True),
        sa.Column('scope_key', sa.String(length=120), nullable=False),
        sa.Column('status', _enum(STATUSES, 'eval_status'), nullable=False),
        sa.Column('trigger', sa.String(length=16), nullable=False),
        sa.Column('requested_by', sa.String(length=64), nullable=False),
        sa.Column('queued_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('catalog_hash', sa.String(length=64), nullable=True),
        sa.Column('formula_version', sa.String(length=16), nullable=True),
        sa.Column('config_version', sa.String(length=32), nullable=True),
        sa.Column('config_hash', sa.String(length=64), nullable=True),
        sa.Column('rule_versions', sa.JSON(), nullable=True),
        sa.Column('examined', sa.JSON(), nullable=True),
        sa.Column('checks_evaluated', sa.Integer(), nullable=False),
        sa.Column('findings_created', sa.Integer(), nullable=False),
        sa.Column('findings_updated', sa.Integer(), nullable=False),
        sa.Column('findings_resolved', sa.Integer(), nullable=False),
        sa.Column('findings_reopened', sa.Integer(), nullable=False),
        sa.Column('findings_unchanged', sa.Integer(), nullable=False),
        sa.Column('subjects_failed', sa.Integer(), nullable=False),
        sa.Column('subjects_skipped', sa.Integer(), nullable=False),
        sa.Column('failures', sa.JSON(), nullable=True),
        sa.Column('truncated', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('truncation_reasons', sa.JSON(), nullable=True),
        sa.Column('aggregate', sa.JSON(), nullable=True),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_quality_eval_runs')),
        sa.UniqueConstraint('run_uid', name=op.f('uq_quality_eval_runs_run_uid')),
    )
    with op.batch_alter_table('quality_eval_runs') as b:
        b.create_index('ix_quality_eval_runs_status', ['status'], unique=False)
        b.create_index('ix_quality_eval_runs_scope', ['scope_type', 'id'], unique=False)
        b.create_index('uq_quality_eval_runs_active_scope', ['scope_key'], unique=True,
                       sqlite_where=sa.text("status IN ('QUEUED','RUNNING')"),
                       postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"))

    op.create_table(
        'quality_scores',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('subject_type', _enum(KINDS, 'score_subject'), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('state', _enum(('SCORED', 'INSUFFICIENT_EVIDENCE'), 'score_state'), nullable=False),
        sa.Column('score', sa.Float(), nullable=True),
        sa.Column('checks_total', sa.Integer(), nullable=False),
        sa.Column('checks_failed', sa.Integer(), nullable=False),
        sa.Column('exceptions_excluded', sa.Integer(), nullable=False),
        sa.Column('applicable_weight', sa.Float(), nullable=False),
        sa.Column('failed_weight', sa.Float(), nullable=False),
        sa.Column('breakdown', sa.JSON(), nullable=True),
        sa.Column('catalog_hash', sa.String(length=64), nullable=False),
        sa.Column('formula_version', sa.String(length=16), nullable=False),
        sa.Column('evaluated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['quality_eval_runs.id'], name=op.f('fk_quality_scores_run_id_quality_eval_runs'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_id'], ['data_sources.id'], name=op.f('fk_quality_scores_source_id_data_sources'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_quality_scores')),
        sa.UniqueConstraint('run_id', 'subject_type', 'subject_id', name='uq_quality_scores_run_subject'),
    )
    with op.batch_alter_table('quality_scores') as b:
        b.create_index('ix_quality_scores_subject', ['subject_type', 'subject_id', 'id'], unique=False)
        b.create_index('ix_quality_scores_source', ['source_id'], unique=False)

    op.create_table(
        'quality_rule_settings',
        sa.Column('rule_id', sa.String(length=16), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.String(length=300), nullable=True),
        sa.Column('updated_by', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('rule_id', name=op.f('pk_quality_rule_settings')),
    )

    with op.batch_alter_table('data_quality_findings') as b:
        b.add_column(sa.Column('origin', _enum(ORIGINS, 'finding_origin'), server_default='INGESTION', nullable=False))
        b.add_column(sa.Column('rule_version', sa.Integer(), nullable=True))
        b.add_column(sa.Column('dimension', sa.String(length=32), nullable=True))
        b.add_column(sa.Column('evidence', sa.JSON(), nullable=True))
        b.add_column(sa.Column('requires_review', sa.Boolean(), server_default=sa.false(), nullable=False))
        b.add_column(sa.Column('first_detected_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('last_evaluated_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('reopened_count', sa.Integer(), server_default='0', nullable=False))
        b.add_column(sa.Column('assigned_to', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('resolved_by', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('run_id', sa.Integer(), nullable=True))
        b.add_column(sa.Column('version', sa.Integer(), server_default='1', nullable=False))
        b.create_foreign_key(op.f('fk_data_quality_findings_run_id_quality_eval_runs'), 'quality_eval_runs', ['run_id'], ['id'], ondelete='SET NULL')
        # widen the lifecycle: replace the old CHECK with the new value list (existing rows are all valid under it)
        b.drop_constraint(b.f('ck_data_quality_findings_finding_resolution'), type_='check')
        b.create_check_constraint('finding_resolution', f"resolution_status IN ({_in(NEW_RESOLUTION)})")
        b.create_index('uq_data_quality_findings_eval_key', ['finding_key'], unique=True,
                       sqlite_where=sa.text("origin = 'EVALUATION'"), postgresql_where=sa.text("origin = 'EVALUATION'"))
        b.create_index('ix_data_quality_findings_origin', ['origin'], unique=False)
        b.create_index('ix_data_quality_findings_run_id', ['run_id'], unique=False)
        b.create_index('ix_data_quality_findings_source_id', ['source_id'], unique=False)

    op.create_table(
        'quality_finding_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('finding_id', sa.Integer(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('actor', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('from_status', sa.String(length=32), nullable=True),
        sa.Column('to_status', sa.String(length=32), nullable=True),
        sa.Column('note', sa.String(length=400), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['finding_id'], ['data_quality_findings.id'], name=op.f('fk_quality_finding_events_finding_id_data_quality_findings'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_quality_finding_events')),
    )
    with op.batch_alter_table('quality_finding_events') as b:
        b.create_index('ix_quality_finding_events_finding', ['finding_id', 'id'], unique=False)

    if op.get_bind().dialect.name == "sqlite":
        for verb in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_quality_finding_events_no_{verb.lower()} BEFORE {verb} ON quality_finding_events "
                "BEGIN SELECT RAISE(ABORT, 'quality_finding_events is append-only'); END"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for verb in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_quality_finding_events_no_{verb}")
    with op.batch_alter_table('quality_finding_events') as b:
        b.drop_index('ix_quality_finding_events_finding')
    op.drop_table('quality_finding_events')
    # Evaluation findings (and their history, dropped above) exist only in this schema version.
    op.execute("DELETE FROM data_quality_findings WHERE origin = 'EVALUATION'")
    op.execute("UPDATE data_quality_findings SET resolution_status = 'OPEN' WHERE resolution_status IN ('IN_REMEDIATION','REOPENED')")
    op.execute("UPDATE data_quality_findings SET resolution_status = 'WONT_FIX' WHERE resolution_status = 'ACCEPTED_EXCEPTION'")
    with op.batch_alter_table('data_quality_findings') as b:
        b.drop_index('ix_data_quality_findings_source_id')
        b.drop_index('ix_data_quality_findings_run_id')
        b.drop_index('ix_data_quality_findings_origin')
        b.drop_index('uq_data_quality_findings_eval_key')
        b.drop_constraint(b.f('ck_data_quality_findings_finding_resolution'), type_='check')
        b.create_check_constraint('finding_resolution', f"resolution_status IN ({_in(OLD_RESOLUTION)})")
        b.drop_constraint(b.f('fk_data_quality_findings_run_id_quality_eval_runs'), type_='foreignkey')
        b.drop_constraint(b.f('ck_data_quality_findings_finding_origin'), type_='check')
        for col in ('version', 'run_id', 'resolved_by', 'assigned_to', 'reopened_count', 'last_evaluated_at',
                    'first_detected_at', 'requires_review', 'evidence', 'dimension', 'rule_version', 'origin'):
            b.drop_column(col)
    op.drop_table('quality_rule_settings')
    with op.batch_alter_table('quality_scores') as b:
        b.drop_index('ix_quality_scores_source')
        b.drop_index('ix_quality_scores_subject')
    op.drop_table('quality_scores')
    with op.batch_alter_table('quality_eval_runs') as b:
        b.drop_index('uq_quality_eval_runs_active_scope')
        b.drop_index('ix_quality_eval_runs_scope')
        b.drop_index('ix_quality_eval_runs_status')
    op.drop_table('quality_eval_runs')
