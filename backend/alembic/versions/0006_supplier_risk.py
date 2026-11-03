"""supplier risk intelligence: runs, assessments, factors, evidence, reviews, alerts

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22 18:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('risk_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_uid', sa.String(length=40), nullable=False),
    sa.Column('scope_type', sa.Enum('SUPPLIER', 'SUPPLIERS', 'NETWORK', name='risk_run_scope', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('scope_ref', sa.JSON(), nullable=True),
    sa.Column('scope_key', sa.String(length=120), nullable=False),
    sa.Column('status', sa.Enum('QUEUED', 'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', name='risk_run_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=200), nullable=True),
    sa.Column('requested_by', sa.String(length=64), nullable=False),
    sa.Column('idempotency_key', sa.String(length=64), nullable=True),
    sa.Column('queued_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('methodology_version', sa.String(length=16), nullable=True),
    sa.Column('methodology_hash', sa.String(length=64), nullable=True),
    sa.Column('graph_snapshot', sa.JSON(), nullable=True),
    sa.Column('examined', sa.Integer(), nullable=False),
    sa.Column('assessed', sa.Integer(), nullable=False),
    sa.Column('partial', sa.Integer(), nullable=False),
    sa.Column('insufficient', sa.Integer(), nullable=False),
    sa.Column('created', sa.Integer(), nullable=False),
    sa.Column('unchanged', sa.Integer(), nullable=False),
    sa.Column('failed', sa.Integer(), nullable=False),
    sa.Column('skipped', sa.Integer(), nullable=False),
    sa.Column('alerts_created', sa.Integer(), nullable=False),
    sa.Column('alerts_updated', sa.Integer(), nullable=False),
    sa.Column('failures', sa.JSON(), nullable=True),
    sa.Column('truncated', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('truncation_reasons', sa.JSON(), nullable=True),
    sa.Column('error', sa.String(length=300), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_runs')),
    sa.UniqueConstraint('idempotency_key', name='uq_risk_runs_idempotency_key'),
    sa.UniqueConstraint('run_uid', name=op.f('uq_risk_runs_run_uid'))
    )
    op.create_index('ix_risk_runs_scope', 'risk_runs', ['scope_type', 'id'], unique=False)
    op.create_index('ix_risk_runs_status', 'risk_runs', ['status'], unique=False)
    op.create_index('uq_risk_runs_active_scope', 'risk_runs', ['scope_key'], unique=True, sqlite_where=sa.text("status IN ('QUEUED','RUNNING')"), postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"))
    op.create_table('risk_assessments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('assessment_uid', sa.String(length=40), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('computed_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.Enum('ASSESSED', 'PARTIAL', 'INSUFFICIENT_EVIDENCE', name='assessment_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('methodology_version', sa.String(length=16), nullable=False),
    sa.Column('methodology_hash', sa.String(length=64), nullable=False),
    sa.Column('risk_score', sa.Float(), nullable=True),
    sa.Column('classification', sa.Enum('LOW', 'MODERATE', 'HIGH', 'CRITICAL', name='assessment_classification', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('evidence_coverage', sa.Float(), nullable=False),
    sa.Column('effective_weight', sa.Float(), nullable=False),
    sa.Column('reliability', sa.Float(), nullable=True),
    sa.Column('reliability_band', sa.Enum('HIGH', 'MODERATE', 'LOW', name='assessment_reliability', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('scored_factors', sa.Integer(), nullable=False),
    sa.Column('contributions', sa.JSON(), nullable=True),
    sa.Column('missing', sa.JSON(), nullable=True),
    sa.Column('actions', sa.JSON(), nullable=True),
    sa.Column('data_basis', sa.JSON(), nullable=True),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('evidence_latest_at', sa.DateTime(), nullable=True),
    sa.Column('evidence_earliest_at', sa.DateTime(), nullable=True),
    sa.Column('requires_review', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('review_state', sa.String(length=24), server_default='UNREVIEWED', nullable=False),
    sa.Column('is_current', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('superseded_at', sa.DateTime(), nullable=True),
    sa.Column('supersedes_id', sa.Integer(), nullable=True),
    sa.Column('last_confirmed_at', sa.DateTime(), nullable=False),
    sa.Column('last_confirmed_run_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("classification IS NULL OR status = 'ASSESSED'", name=op.f('ck_risk_assessments_classified_only_when_assessed')),
    sa.CheckConstraint("status <> 'INSUFFICIENT_EVIDENCE' OR (risk_score IS NULL AND classification IS NULL)", name=op.f('ck_risk_assessments_insufficient_has_no_score')),
    sa.CheckConstraint("status = 'INSUFFICIENT_EVIDENCE' OR risk_score IS NOT NULL", name=op.f('ck_risk_assessments_scored_has_score')),
    sa.CheckConstraint('evidence_coverage >= 0 AND evidence_coverage <= 1', name=op.f('ck_risk_assessments_coverage_range')),
    sa.CheckConstraint('reliability IS NULL OR (reliability >= 0 AND reliability <= 1)', name=op.f('ck_risk_assessments_reliability_range')),
    sa.CheckConstraint('risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)', name=op.f('ck_risk_assessments_score_range')),
    sa.ForeignKeyConstraint(['run_id'], ['risk_runs.id'], name=op.f('fk_risk_assessments_run_id_risk_runs'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['supersedes_id'], ['risk_assessments.id'], name=op.f('fk_risk_assessments_supersedes_id_risk_assessments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_risk_assessments_supplier_id_suppliers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_assessments')),
    sa.UniqueConstraint('assessment_uid', name=op.f('uq_risk_assessments_assessment_uid')),
    sa.UniqueConstraint('run_id', 'supplier_id', name='uq_risk_assessments_run_supplier')
    )
    op.create_index('ix_risk_assessments_classification', 'risk_assessments', ['classification'], unique=False)
    op.create_index('ix_risk_assessments_current', 'risk_assessments', ['is_current', 'risk_score'], unique=False)
    op.create_index('ix_risk_assessments_run', 'risk_assessments', ['run_id'], unique=False)
    op.create_index('ix_risk_assessments_status', 'risk_assessments', ['status'], unique=False)
    op.create_index('ix_risk_assessments_supplier', 'risk_assessments', ['supplier_id', 'id'], unique=False)
    op.create_index('uq_risk_assessments_current_supplier', 'risk_assessments', ['supplier_id'], unique=True, sqlite_where=sa.text('is_current = 1'), postgresql_where=sa.text('is_current'))
    op.create_table('risk_alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('alert_key', sa.String(length=120), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=False),
    sa.Column('assessment_id', sa.Integer(), nullable=True),
    sa.Column('alert_type', sa.Enum('SCORE_INCREASE', 'NEW_SINGLE_SOURCE', 'EVIDENCE_STALE', 'RELATIONSHIP_UNVERIFIED', 'ASSESSMENT_UNAVAILABLE', 'METHODOLOGY_CHANGE', name='risk_alert_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('severity', sa.Enum('INFO', 'WARNING', 'ERROR', 'CRITICAL', name='risk_alert_severity', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('status', sa.Enum('OPEN', 'ACKNOWLEDGED', 'DISMISSED', 'RESOLVED', name='risk_alert_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('reason', sa.String(length=400), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=True),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.Column('occurrences', sa.Integer(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_by', sa.String(length=64), nullable=True),
    sa.Column('resolution_note', sa.String(length=400), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.ForeignKeyConstraint(['assessment_id'], ['risk_assessments.id'], name=op.f('fk_risk_alerts_assessment_id_risk_assessments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_risk_alerts_supplier_id_suppliers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_alerts'))
    )
    op.create_index('ix_risk_alerts_status', 'risk_alerts', ['status', 'alert_type'], unique=False)
    op.create_index('ix_risk_alerts_supplier', 'risk_alerts', ['supplier_id', 'id'], unique=False)
    op.create_index('uq_risk_alerts_active_key', 'risk_alerts', ['alert_key'], unique=True, sqlite_where=sa.text("status IN ('OPEN','ACKNOWLEDGED')"), postgresql_where=sa.text("status IN ('OPEN','ACKNOWLEDGED')"))
    op.create_table('risk_factors',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('assessment_id', sa.Integer(), nullable=False),
    sa.Column('category', sa.Enum('CONCENTRATION', 'DEPENDENCY', 'GEOGRAPHIC', 'OPERATIONAL', 'DELIVERY', 'FINANCIAL', 'COMPLIANCE', 'DATA_QUALITY', 'SINGLE_SOURCE', 'RELATIONSHIP_UNCERTAINTY', name='factor_category', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('role', sa.String(length=12), nullable=False),
    sa.Column('state', sa.Enum('SCORED', 'UNAVAILABLE', 'NOT_APPLICABLE', name='factor_state', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('basis', sa.Enum('OBSERVED', 'INFERRED', 'UNKNOWN', name='factor_basis', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('severity', sa.Enum('LOW', 'MODERATE', 'HIGH', 'CRITICAL', name='factor_severity', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('weight', sa.Float(), nullable=False),
    sa.Column('explanation', sa.String(length=800), nullable=False),
    sa.Column('unavailable_reason', sa.String(length=400), nullable=True),
    sa.Column('missing_inputs', sa.JSON(), nullable=True),
    sa.Column('evidence_observed_at', sa.DateTime(), nullable=True),
    sa.Column('freshness', sa.Enum('FRESH', 'AGING', 'STALE', 'UNKNOWN', name='factor_freshness', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('quality_context', sa.JSON(), nullable=True),
    sa.Column('requires_review', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('details', sa.JSON(), nullable=True),
    sa.Column('method_version', sa.String(length=16), nullable=False),
    sa.CheckConstraint("(state = 'SCORED') = (score IS NOT NULL)", name=op.f('ck_risk_factors_score_iff_scored')),
    sa.CheckConstraint('confidence IS NULL OR (confidence >= 0 AND confidence <= 1)', name=op.f('ck_risk_factors_confidence_range')),
    sa.CheckConstraint('score IS NULL OR (score >= 0 AND score <= 100)', name=op.f('ck_risk_factors_score_range')),
    sa.ForeignKeyConstraint(['assessment_id'], ['risk_assessments.id'], name=op.f('fk_risk_factors_assessment_id_risk_assessments'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_factors')),
    sa.UniqueConstraint('assessment_id', 'category', name='uq_risk_factors_assessment_category')
    )
    op.create_index('ix_risk_factors_assessment', 'risk_factors', ['assessment_id'], unique=False)
    op.create_index('ix_risk_factors_category_state', 'risk_factors', ['category', 'state'], unique=False)
    op.create_table('risk_reviews',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=False),
    sa.Column('assessment_id', sa.Integer(), nullable=False),
    sa.Column('action', sa.Enum('ACKNOWLEDGE', 'NOTE', 'OVERRIDE', 'REVOKE_OVERRIDE', 'REQUEST_EVIDENCE', 'FOLLOW_UP', name='review_action', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('factor_category', sa.String(length=32), nullable=True),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('actor_role', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('note', sa.String(length=1000), nullable=True),
    sa.Column('override_classification', sa.Enum('LOW', 'MODERATE', 'HIGH', 'CRITICAL', name='review_override_class', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('override_expires_at', sa.DateTime(), nullable=True),
    sa.Column('target_review_id', sa.Integer(), nullable=True),
    sa.Column('original_status', sa.String(length=24), nullable=False),
    sa.Column('original_classification', sa.String(length=16), nullable=True),
    sa.Column('original_score', sa.Float(), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.CheckConstraint("action <> 'OVERRIDE' OR (override_classification IS NOT NULL AND note IS NOT NULL AND length(trim(note)) > 0)", name=op.f('ck_risk_reviews_override_needs_class_and_note')),
    sa.CheckConstraint("action <> 'REVOKE_OVERRIDE' OR target_review_id IS NOT NULL", name=op.f('ck_risk_reviews_revoke_needs_target')),
    sa.CheckConstraint("action = 'OVERRIDE' OR override_classification IS NULL", name=op.f('ck_risk_reviews_class_only_on_override')),
    sa.ForeignKeyConstraint(['assessment_id'], ['risk_assessments.id'], name=op.f('fk_risk_reviews_assessment_id_risk_assessments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_risk_reviews_supplier_id_suppliers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_reviews'))
    )
    op.create_index('ix_risk_reviews_assessment', 'risk_reviews', ['assessment_id', 'id'], unique=False)
    op.create_index('ix_risk_reviews_supplier', 'risk_reviews', ['supplier_id', 'id'], unique=False)
    op.create_table('risk_evidence',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('assessment_id', sa.Integer(), nullable=False),
    sa.Column('factor_id', sa.Integer(), nullable=False),
    sa.Column('evidence_type', sa.Enum('REGISTRY_FIELD', 'RELATIONSHIP', 'QUALITY_FINDING', 'QUALITY_SCORE', 'OBSERVATION', 'IMPORT_RECORD', 'ANALYST_NOTE', 'EXTERNAL_OBSERVATION', 'MODEL_OUTPUT', name='evidence_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('ref', sa.String(length=80), nullable=False),
    sa.Column('summary', sa.String(length=400), nullable=False),
    sa.Column('observed_at', sa.DateTime(), nullable=True),
    sa.Column('provenance', sa.String(length=24), nullable=True),
    sa.Column('source_namespace', sa.String(length=80), nullable=True),
    sa.Column('reasoning', sa.String(length=400), nullable=False),
    sa.ForeignKeyConstraint(['assessment_id'], ['risk_assessments.id'], name=op.f('fk_risk_evidence_assessment_id_risk_assessments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['factor_id'], ['risk_factors.id'], name=op.f('fk_risk_evidence_factor_id_risk_factors'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_evidence')),
    sa.UniqueConstraint('factor_id', 'evidence_type', 'ref', name='uq_risk_evidence_factor_ref')
    )
    op.create_index('ix_risk_evidence_assessment', 'risk_evidence', ['assessment_id'], unique=False)
    op.create_index('ix_risk_evidence_ref', 'risk_evidence', ['evidence_type', 'ref'], unique=False)

    if op.get_bind().dialect.name == "sqlite":
        for verb in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_risk_reviews_no_{verb.lower()} BEFORE {verb} ON risk_reviews "
                "BEGIN SELECT RAISE(ABORT, 'risk_reviews is append-only'); END"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for verb in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_risk_reviews_no_{verb}")
    for table in ("risk_alerts", "risk_reviews", "risk_evidence", "risk_factors", "risk_assessments", "risk_runs"):
        op.drop_table(table)
