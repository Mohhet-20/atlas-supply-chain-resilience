"""identity: organisations, users, sessions, reset tokens, login throttles; organisation scope for alerting and audit

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-02

Additive and reversible. The default organisation (id 1) is created and every existing alerting row is assigned to it, so
nothing existing becomes invalid. audit_events only gets a nullable column added in place (ALTER TABLE ADD COLUMN): the table
carries append-only triggers and a hash chain, so it is never recreated or rewritten.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORG_FK = "fk_{t}_organization_id_organizations"


def _scope(table: str, *, drop: tuple[str, ...] = (), create: tuple[tuple[str, list[str]], ...] = ()) -> None:
    with op.batch_alter_table(table, schema=None) as b:
        b.add_column(sa.Column("organization_id", sa.Integer(), server_default="1", nullable=False))
        b.create_foreign_key(ORG_FK.format(t=table), "organizations", ["organization_id"], ["id"])
        for name in drop:
            b.drop_constraint(name, type_="unique")
        for name, cols in create:
            b.create_unique_constraint(name, cols)
        b.create_index(f"ix_{table}_organization_id", ["organization_id"], unique=False)


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_uid", sa.String(length=40), nullable=False),
        sa.Column("slug", sa.String(length=48), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint("status in ('active', 'suspended')", name=op.f("ck_organizations_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("org_uid", name=op.f("uq_organizations_org_uid")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
    )
    op.execute(
        "INSERT INTO organizations (id, org_uid, slug, name, status, created_at, created_by) "
        "VALUES (1, 'OR-DEFAULT', 'default', 'Default organization', 'active', CURRENT_TIMESTAMP, 'migration')"
    )
    if op.get_bind().dialect.name == "postgresql":  # an explicit id does not advance the serial sequence there
        op.execute("SELECT setval(pg_get_serial_sequence('organizations', 'id'), (SELECT MAX(id) FROM organizations))")
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_uid", sa.String(length=40), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("platform_admin", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("password_hash", sa.String(length=300), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("session_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status in ('active', 'disabled', 'suspended', 'pending')", name=op.f("ck_users_status")),
        sa.CheckConstraint("role in ('viewer', 'editor', 'reviewer', 'admin')", name=op.f("ck_users_role")),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], name=op.f("fk_users_organization_id_organizations")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("user_uid", name=op.f("uq_users_user_uid")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_index("ix_users_org", "users", ["organization_id"])
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.String(length=40), nullable=True),
        sa.Column("client_hash", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_auth_sessions_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
    )
    op.create_index("ix_auth_sessions_user", "auth_sessions", ["user_id"])
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("issued_by", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_password_reset_tokens_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_password_reset_tokens_token_hash")),
    )
    op.create_index("ix_password_reset_user", "password_reset_tokens", ["user_id"])
    op.create_table(
        "login_throttles",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("key_hash", name=op.f("pk_login_throttles")),
    )
    _scope("alert_events", drop=("uq_alert_events_idempotency",), create=(("uq_alert_events_idempotency", ["organization_id", "source", "idempotency_key"]),))
    _scope("alert_escalation_policies", drop=("uq_alert_escalation_policies_name",), create=(("uq_alert_policies_org_name", ["organization_id", "name"]),))
    _scope("alert_rules", drop=("uq_alert_rules_name",), create=(("uq_alert_rules_org_name", ["organization_id", "name"]),))
    _scope("alerts")
    _scope("alert_suppressions")
    op.add_column("audit_events", sa.Column("organization_id", sa.Integer(), nullable=True))
    op.create_index("ix_audit_events_org", "audit_events", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_org", table_name="audit_events")
    with op.batch_alter_table("audit_events", recreate="never") as b:
        b.drop_column("organization_id")
    for table, restore in (
        ("alert_suppressions", None),
        ("alerts", None),
        ("alert_rules", ("uq_alert_rules_org_name", "uq_alert_rules_name", ["name"])),
        ("alert_escalation_policies", ("uq_alert_policies_org_name", "uq_alert_escalation_policies_name", ["name"])),
        ("alert_events", ("uq_alert_events_idempotency", "uq_alert_events_idempotency", ["source", "idempotency_key"])),
    ):
        with op.batch_alter_table(table, schema=None) as b:
            b.drop_index(f"ix_{table}_organization_id")
            if restore:
                b.drop_constraint(restore[0], type_="unique")
                b.create_unique_constraint(restore[1], restore[2])
            b.drop_constraint(ORG_FK.format(t=table), type_="foreignkey")
            b.drop_column("organization_id")
    op.drop_table("login_throttles")
    op.drop_index("ix_password_reset_user", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index("ix_auth_sessions_user", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_users_org", table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")
