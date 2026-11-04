"""Identity authorization regression tests: deny-by-default routes, role/permission map agreement, privilege-escalation guards."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.core.auth import Actor, Role
from app.core.permissions import ROLE_PERMISSIONS, Permission, permissions_of
from tests.quality_helpers import h
from tests.security_helpers import ADM, bearer, make_app, mk_org, mk_user

PUBLIC = {
    "/api/v1/health",
    "/api/v1/readiness",
    "/api/v1/auth/login",
    "/api/v1/auth/guest",  # public by design; 404 unless guest access is enabled (tests/test_guest_access.py)
    "/api/v1/auth/password-reset/request",
    "/api/v1/auth/password-reset/confirm",
    "/api/v1/webhooks/inbound/{uid}",  # authenticated by HMAC signature, not a session
}


@pytest.fixture
def client(settings, engine):
    with TestClient(make_app(settings, engine)) as c:
        mk_user(c, "alice", "admin")
        mk_user(c, "vera", "viewer")
        yield c


def _routes(c):
    for path, ops in c.get("/openapi.json").json()["paths"].items():
        for m in ops:
            if m in ("get", "post", "put", "patch", "delete"):
                yield m.upper(), path, re.sub(r"\{[^}]+\}", "x1234", path)


def test_every_route_but_the_public_ones_requires_authentication(client):
    leaks = []
    for m, path, url in _routes(client):
        if path in PUBLIC:
            continue
        r = client.request(m, url, json={} if m != "GET" else None)
        if r.status_code != 401:
            leaks.append((m, path, r.status_code))
    assert leaks == []  # regression for V1: 34 routes were reachable without a token


def test_viewer_cannot_reach_any_write_route(client):
    vh = h("viewer")
    reachable = []
    for m, path, url in _routes(client):
        if m == "GET" or path in PUBLIC or path.endswith(("/logout", "/logout-all", "/password")):
            continue
        r = client.request(m, url, json={}, headers=vh)
        if r.status_code != 403:
            reachable.append((m, path, r.status_code))
    # notification read/preferences are per-user viewer actions; everything else must be refused
    # stateless POST-bodied computations (validate / preview / calc / compare / ranking / eligibility); they persist nothing
    stateless = r"/(validate|preview|compare|ranking|eligibility|alternatives|availability|calc/[a-z-]+)$"
    allowed = re.compile(r"/alerting/(notifications|preferences)|" + stateless.lstrip("/"))
    assert [x for x in reachable if not allowed.search(x[1])] == []


def test_admin_surface_refused_to_non_admins(client):
    for role in ("viewer", "editor", "reviewer"):
        assert client.get(f"{ADM}/users", headers=h(role)).status_code == 403
    for role in ("viewer", "editor"):  # the security summary needs audit.read (reviewer and above)
        assert client.get(f"{ADM}/security/summary", headers=h(role)).status_code == 403
    assert client.get(f"{ADM}/security/summary", headers=h("reviewer")).status_code == 200


def test_role_permission_map_is_monotonic_and_deny_by_default():
    sets = [permissions_of(Actor("t", r, True)) for r in (Role.VIEWER, Role.EDITOR, Role.REVIEWER, Role.ADMIN)]
    for lo, hi in zip(sets, sets[1:], strict=False):
        assert lo < hi  # each higher role strictly extends the lower one
    admin = permissions_of(Actor("t", Role.ADMIN, True))
    assert Permission.ORG_MANAGE not in admin and Permission.SYSTEM_ADMIN not in admin
    assert {Permission.ORG_MANAGE, Permission.SYSTEM_ADMIN} <= permissions_of(
        Actor("t", Role.ADMIN, True, platform_admin=True)
    )
    assert Permission.USER_MANAGE not in permissions_of(Actor("t", Role.REVIEWER, True))
    assert set(ROLE_PERMISSIONS) == set(Role)


@pytest.mark.parametrize(
    "role,write_path,expected",
    [
        ("viewer", "/api/v1/alerting/events", 403),
        ("editor", "/api/v1/alerting/rules", 403),
        ("reviewer", f"{ADM}/users", 403),
    ],
)
def test_route_roles_agree_with_the_permission_map(client, role, write_path, expected):
    assert client.post(write_path, json={}, headers=h(role)).status_code == expected


def test_admin_cannot_change_own_role_or_status(client):
    ah = bearer(client, "alice")
    me = {u["username"]: u for u in client.get(f"{ADM}/users", headers=ah).json()["items"]}["alice"]
    assert client.patch(f"{ADM}/users/{me['user_uid']}", json={"role": "viewer"}, headers=ah).status_code == 403
    assert client.patch(f"{ADM}/users/{me['user_uid']}", json={"status": "disabled"}, headers=ah).status_code in (
        403,
        409,
    )


def test_last_active_admin_cannot_be_removed(client):
    mk_user(client, "bob", "admin")
    bh = bearer(client, "bob")
    users = {u["username"]: u for u in client.get(f"{ADM}/users", headers=bh).json()["items"]}
    assert (
        client.patch(f"{ADM}/users/{users['alice']['user_uid']}", json={"status": "disabled"}, headers=bh).status_code
        == 200
    )
    # bob is now the only active admin and cannot be demoted by anyone
    assert (
        client.patch(f"{ADM}/users/{users['bob']['user_uid']}", json={"role": "viewer"}, headers=bh).status_code == 403
    )


def test_cannot_grant_a_role_above_your_own_or_platform_admin(client):
    mk_user(client, "rita", "reviewer")
    rh = bearer(client, "rita")
    ah = bearer(client, "alice")
    # reviewer has no user.manage at all
    assert (
        client.post(
            f"{ADM}/users",
            json={"username": "x-user", "password": "Synthetic-Pass-Phrase-41!", "role": "viewer"},
            headers=rh,
        ).status_code
        == 403
    )
    body = {"username": "plat-user", "password": "Synthetic-Pass-Phrase-41!", "role": "viewer", "platform_admin": True}
    assert client.post(f"{ADM}/users", json=body, headers=ah).status_code == 403
    assert client.post(f"{ADM}/users", json=body | {"platform_admin": False}, headers=ah).status_code in (200, 201)


def test_org_admin_cannot_manage_other_orgs_or_create_orgs(client):
    other = mk_org(client, "other")
    mk_user(client, "olga", "admin", org_id=other)
    ah = bearer(client, "alice")
    assert (
        client.post(
            f"{ADM}/organizations",
            json={
                "slug": "evil",
                "name": "Evil",
                "admin_username": "evil-admin",
                "admin_password": "Synthetic-Pass-Phrase-41!",
            },
            headers=ah,
        ).status_code
        == 403
    )
    orgs = client.get(f"{ADM}/organizations", headers=ah).json()["items"]
    assert [o["slug"] for o in orgs] == ["default"] or len(orgs) == 1  # an org admin sees only their own
    users = client.get(f"{ADM}/users", headers=ah).json()["items"]
    assert "olga" not in {u["username"] for u in users}
    assert all(u["organization_id"] == 1 for u in users)


def test_cross_org_user_lookup_is_indistinguishable_from_missing(client):
    other = mk_org(client, "other")
    olga = mk_user(client, "olga", "admin", org_id=other)
    ah = bearer(client, "alice")
    a = client.get(f"{ADM}/users/{olga}", headers=ah)
    b = client.get(f"{ADM}/users/US-DOESNOTEXIST", headers=ah)
    assert a.status_code == b.status_code == 404
    assert a.json()["error"]["message"] == b.json()["error"]["message"]
    assert client.patch(f"{ADM}/users/{olga}", json={"role": "viewer"}, headers=ah).status_code == 404
    assert client.post(f"{ADM}/users/{olga}/password-reset", headers=ah).status_code == 404
    assert client.post(f"{ADM}/users/{olga}/revoke-sessions", headers=ah).status_code == 404


def test_suspended_organisation_blocks_login_and_sessions(client):
    other = mk_org(client, "other")
    mk_user(client, "olga", "admin", org_id=other)
    oh = bearer(client, "olga")
    from app.auth import service as svc
    from app.models.identity import Organization
    from tests.security_helpers import OPERATOR

    with client.app.state.session_factory() as db:
        svc.set_org_status(db, OPERATOR, db.get(Organization, other), "suspended")
        db.commit()
    assert client.get("/api/v1/auth/me", headers=oh).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "olga", "password": "Synthetic-Pass-Phrase-41!"}
        ).status_code
        == 401
    )
