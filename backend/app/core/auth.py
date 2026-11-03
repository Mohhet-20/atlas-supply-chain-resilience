"""Request authentication and role checks.

A request is authenticated by one of three credentials: a user session token (``atl_s_...``, see ``app.auth``), a
service-account API key (``atl_k_...``, see ``app.integrations.apikeys``), or a static token from ``ATLAS_API_TOKENS``
(only SHA-256 digests are kept in memory). The resolved actor is stored on ``request.state`` so the request's database
session is scoped to the actor's organisation (``app.db.tenancy``).

Without users or static tokens:
- development/test: requests run as the unauthenticated ``dev-local`` admin (audited as such); the first user account
  withdraws it;
- production: requests are refused with 503 ``auth_not_configured``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Annotated

from fastapi import Depends, Request

from app.core.errors import AuthNotConfiguredError, ForbiddenError, UnauthenticatedError


class Role(IntEnum):
    VIEWER = 1
    EDITOR = 2
    REVIEWER = 3
    ADMIN = 4


@dataclass(frozen=True)
class Actor:
    name: str
    role: Role
    authenticated: bool
    org_id: int = (
        1  # organisation the request is scoped to; static-token and development actors belong to the default one
    )
    user_id: int | None = None  # set for session users; None for service (API-token) actors
    platform_admin: bool = False
    session_id: int | None = None
    scopes: frozenset[str] | None = None  # API keys only: narrows permission-checked routes below the role's defaults
    api_key_id: int | None = None


DEV_ACTOR = Actor("dev-local", Role.ADMIN, False)
_ACTOR_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,128}\Z")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_token_table(raw: str) -> dict[str, Actor]:
    """Parse ``token:actor:role`` entries into ``{sha256(token): Actor}``. Never echoes token values in errors."""
    table: dict[str, Actor] = {}
    for index, entry in enumerate(e for e in raw.split(",") if e.strip()):
        parts = entry.strip().split(":")
        if len(parts) != 3:
            raise ValueError(f"api_tokens entry {index + 1} must look like token:actor:role")
        token, name, role_text = parts
        if not _TOKEN_RE.match(token):
            raise ValueError(f"api_tokens entry {index + 1}: token must be 16-128 URL-safe characters")
        if not _ACTOR_RE.match(name):
            raise ValueError(f"api_tokens entry {index + 1}: invalid actor name")
        try:
            role = Role[role_text.strip().upper()]
        except KeyError:
            raise ValueError(f"api_tokens entry {index + 1}: role must be viewer, editor, reviewer or admin") from None
        key = _digest(token)
        if key in table:
            raise ValueError(f"api_tokens entry {index + 1}: duplicate token")
        table[key] = Actor(name, role, True)
    return table


def token_table(request: Request) -> dict[str, Actor]:
    settings = request.app.state.settings
    cached = getattr(request.app.state, "_token_table", None)
    if cached is None:
        raw = settings.api_tokens.get_secret_value() if settings.api_tokens else ""
        cached = parse_token_table(raw) if raw else {}
        request.app.state._token_table = cached
    return cached


def current_actor(request: Request) -> Actor:
    """Authenticate the request and record the actor on ``request.state`` so the per-request database session can scope
    every query to the actor's organisation (``app.db.tenancy``)."""
    actor = _resolve_actor(request)
    request.state.actor = actor
    return actor


def _resolve_actor(request: Request) -> Actor:
    """Static API tokens (service accounts, default organisation) or a user session token (``atl_s_...``)."""
    settings = request.app.state.settings
    table = token_table(request)
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    has_bearer = scheme.lower() == "bearer" and bool(token)
    if token.startswith("atl_s_") and has_bearer:  # a user session
        from app.auth import service as auth_service

        with request.app.state.session_factory() as db, db.begin():
            actor = auth_service.resolve_session(db, settings, token)
        if actor is None:
            raise UnauthenticatedError("The session is not valid or has expired.")
        return actor
    if token.startswith("atl_k_") and has_bearer:  # a scoped API key of a service account
        from app.integrations import apikeys

        with request.app.state.session_factory() as db, db.begin():
            actor = apikeys.resolve_key(db, token)
        if actor is None:
            raise UnauthenticatedError("The API key is not valid, expired or revoked.")
        return actor
    if not table:
        if settings.production_like:
            if _users_exist(request):
                raise UnauthenticatedError("A bearer token is required.")
            raise AuthNotConfiguredError(
                "Access requires user accounts or ATLAS_API_TOKENS to be configured in production."
            )
        if has_bearer or not _users_exist(request):
            return DEV_ACTOR
        raise UnauthenticatedError("A bearer token is required.")
    if not has_bearer:
        raise UnauthenticatedError("A bearer token is required.")
    candidate = _digest(token)
    found: Actor | None = None
    for key, actor in table.items():  # constant-time comparison against every stored digest
        if hmac.compare_digest(key, candidate):
            found = actor
    if found is None:
        raise UnauthenticatedError("The bearer token is not valid.")
    return found


def _users_exist(request: Request) -> bool:
    """Development convenience only: once any user account exists the open dev actor is withdrawn."""
    from sqlalchemy import select

    from app.models.identity import User

    with request.app.state.session_factory() as db:
        return db.scalar(select(User.id).limit(1)) is not None


CurrentActor = Annotated[Actor, Depends(current_actor)]


def require(role: Role):
    """Dependency factory enforcing a minimum role. Denials by authenticated actors are audited."""

    def _dependency(request: Request, actor: CurrentActor) -> Actor:
        if actor.role < role:
            from app.registry.audit import record_denied

            record_denied(request, actor, f"requires role {role.name.lower()}")
            raise ForbiddenError(f"This action requires the {role.name.lower()} role.")
        return actor

    return _dependency
