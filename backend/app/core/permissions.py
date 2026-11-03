"""Central permission model: named permissions, a role-to-permission map and a deny-by-default check.

The four existing roles (viewer < editor < reviewer < admin) are kept; adding more would give no extra least-privilege
and would change every existing route. Each permission is granted to the *lowest* role that needs it and to every role above.
``platform_admin`` is a flag on a user (not a fifth role) that additionally allows managing organisations.

Existing routes still use ``require(Role.X)``; the test suite checks that each of those gates agrees with this map
(see tests/test_security_permissions.py), so the map is the documented source of truth, not decoration.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from fastapi import Depends, Request

from app.core.auth import Actor, CurrentActor, Role
from app.core.errors import ForbiddenError


class Permission(str, Enum):
    # reads
    DATA_READ = "data.read"  # suppliers, facilities, products, network, observations, lineage
    ANALYTICS_READ = "analytics.read"  # risk, scenarios, inventory, forecast, optimisation, dashboards (results)
    ALERT_READ = "alert.read"
    # writes
    DATA_WRITE = "data.write"  # registry edits, relationships
    INGEST = "ingest.write"  # file ingestion and mapping profiles
    ANALYTICS_RUN = "analytics.run"  # run assessments, scenarios, forecasts, optimisations
    ALERT_WRITE = "alert.write"  # ingest events, acknowledge, assign, resolve, notes
    ALERT_ADMIN = "alert.admin"  # rules, policies, suppressions, close/reopen, evaluate
    EXPORT = "export.create"
    APPROVE = "decision.approve"  # decision approvals, model promotion, merges, mapping activation
    # governance
    AUDIT_READ = "audit.read"
    USER_MANAGE = "user.manage"  # users of the actor's organisation
    INTEGRATION_READ = "integration.read"  # integration registry, runs, health, webhook deliveries (no secrets)
    INTEGRATION_ADMIN = "integration.admin"  # webhooks and external destinations
    ORG_MANAGE = "org.manage"  # organisations (platform administrators only)
    SYSTEM_ADMIN = "system.admin"  # platform-wide settings and cross-organisation audit (platform administrators only)


P = Permission
_VIEWER = {P.DATA_READ, P.ANALYTICS_READ, P.ALERT_READ}
_EDITOR = _VIEWER | {P.DATA_WRITE, P.INGEST, P.ANALYTICS_RUN, P.ALERT_WRITE, P.EXPORT}
_REVIEWER = _EDITOR | {P.ALERT_ADMIN, P.APPROVE, P.AUDIT_READ, P.INTEGRATION_READ}
_ADMIN = _REVIEWER | {P.USER_MANAGE, P.INTEGRATION_ADMIN}
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_VIEWER),
    Role.EDITOR: frozenset(_EDITOR),
    Role.REVIEWER: frozenset(_REVIEWER),
    Role.ADMIN: frozenset(_ADMIN),
}
PLATFORM_ONLY = frozenset({P.ORG_MANAGE, P.SYSTEM_ADMIN})


def permissions_of(actor: Actor) -> frozenset[Permission]:
    granted = ROLE_PERMISSIONS.get(actor.role, frozenset())
    if actor.scopes:  # an API key can only ever hold fewer permissions than its role, never more
        granted = frozenset(p for p in granted if p.value in actor.scopes)
    return granted | PLATFORM_ONLY if actor.platform_admin else granted


def has(actor: Actor, permission: Permission) -> bool:
    return permission in permissions_of(actor)


def require_permission(permission: Permission):
    """Dependency factory: deny unless the actor holds the permission. Denials are audited like role denials."""

    def _dependency(request: Request, actor: CurrentActor) -> Actor:
        if not has(actor, permission):
            from app.registry.audit import record_denied

            record_denied(request, actor, f"requires permission {permission.value}")
            raise ForbiddenError(f"This action requires the {permission.value} permission.")
        return actor

    return _dependency


def Allowed(permission: Permission):  # noqa: N802 - used as a type alias factory in route signatures
    return Annotated[Actor, Depends(require_permission(permission))]
