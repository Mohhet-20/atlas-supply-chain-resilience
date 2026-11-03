"""Secret handling without storing secrets.

* Integration credentials are *references* ``env:ATLAS_CRED_<NAME>`` resolved from the process environment at call time. Only the
  ``ATLAS_CRED_`` namespace is readable, so a reference can never be used to read ``ATLAS_API_TOKENS`` or any other setting.
* Webhook signing secrets are derived: ``HMAC-SHA256(master_key, kind:endpoint_uid:version)``. The database stores only the
  version, rotation increments it, and the secret is shown to the caller once (on create/rotate) and never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

from app.core.config import Settings
from app.core.errors import AtlasError, DomainValidationError

_REF = re.compile(r"^env:(ATLAS_CRED_[A-Z0-9_]{1,56})$")


class NotConfiguredError(AtlasError):
    status_code = 503
    code = "not_configured"


def validate_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not _REF.match(ref):
        raise DomainValidationError(
            "credential_ref must look like env:ATLAS_CRED_NAME (the secret itself is never stored)."
        )
    return ref


def resolve_ref(ref: str | None) -> str | None:
    """The secret value, or None when there is no reference or the variable is unset. Never log the return value."""
    m = _REF.match(ref or "")
    return os.environ.get(m.group(1)) if m else None


def credential_status(ref: str | None) -> str:
    """What the API may reveal: whether a credential is wired up, never what it is."""
    if not ref:
        return "none"
    return "available" if resolve_ref(ref) else "missing"


def _master(cfg: Settings) -> bytes:
    if cfg.integrations_master_key is None:
        raise NotConfiguredError("Webhooks need ATLAS_INTEGRATIONS_MASTER_KEY to be configured.")
    return cfg.integrations_master_key.get_secret_value().encode()


def derive_secret(cfg: Settings, kind: str, uid: str, version: int) -> str:
    mac = hmac.new(_master(cfg), f"{kind}:{uid}:{version}".encode(), hashlib.sha256).digest()
    return "whsec_" + base64.urlsafe_b64encode(mac).decode().rstrip("=")


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, header: str, body: bytes, now: int, tolerance: int) -> bool:
    """Constant-time verification of ``t=<unix>,v1=<hex>`` with a replay window."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(now - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)
