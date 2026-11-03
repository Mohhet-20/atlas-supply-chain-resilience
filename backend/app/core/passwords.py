"""Password hashing and policy using only the standard library (scrypt, memory-hard) - no new dependency.

Stored format: ``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>``. The parameters travel with the hash, so the work factor can be
raised later and old hashes still verify (``needs_rehash`` reports when a login should upgrade one). Verification is
constant-time and a dummy hash is checked for unknown users so timing does not reveal whether an account exists.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from app.core.errors import DomainValidationError

_MAXMEM = 256 * 1024 * 1024
_DKLEN = 32
# A short deny-list of the most common passwords; the length rule and the "not the username" rule do the rest.
COMMON = frozenset(
    "password passw0rd password1 password12 password123 password1234 123456789012 1234567890123 qwertyuiop12 "
    "qwerty123456 letmein123456 administrator welcome12345 welcome123456 changeme1234 iloveyou1234 atlas1234567 "
    "atlasatlasatlas abcdefghijkl abc123abc123 000000000000 111111111111 admin1234567 adminadmin12".split()
)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str, n: int = 2**15, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_DKLEN)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
            dklen=len(expected),
        )
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str, n: int, r: int, p: int) -> bool:
    try:
        _, sn, sr, sp, _, _ = stored.split("$")
        return (int(sn), int(sr), int(sp)) != (n, r, p)
    except ValueError:
        return True


_DUMMY: dict[tuple[int, int, int], str] = {}


def dummy_verify(password: str, n: int, r: int, p: int) -> None:
    """Spend the same time as a real check so an unknown username is not distinguishable by latency."""
    key = (n, r, p)
    if key not in _DUMMY:
        _DUMMY[key] = hash_password("not-a-real-password", n, r, p)
    verify_password(password, _DUMMY[key])


def validate_policy(password: str, *, username: str = "", min_length: int = 12) -> None:
    """Raise DomainValidationError listing every unmet rule. The message never echoes the password."""
    problems: list[str] = []
    if len(password) < min_length:
        problems.append(f"must be at least {min_length} characters")
    if len(password) > 128:
        problems.append("must be at most 128 characters")
    low = password.lower()
    if low in COMMON or low.strip("0123456789") in {"password", "qwerty", "letmein", "welcome", "admin", "atlas"}:
        problems.append("is too common")
    if username and len(username) >= 3 and username.lower() in low:
        problems.append("must not contain the username")
    if len(set(password)) < 5:
        problems.append("must use at least 5 different characters")
    if problems:
        raise DomainValidationError("Password " + "; ".join(problems) + ".")
