"""Deterministic normalization used for matching. Originals are always stored unchanged next to these values."""

from __future__ import annotations

import re
import unicodedata

# Legal-form words that must not, on their own, make two organisations look the same (or different).
LEGAL_FORMS = frozenset(
    {
        "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "llp", "lp",
        "plc", "gmbh", "ag", "kg", "sa", "sas", "srl", "spa", "bv", "nv", "ab", "oy", "as", "pte", "pvt",
        "private", "public", "holdings", "holding", "group", "the", "and",
    }
)  # fmt: skip

# "G.m.b.H." / "S.A." -> "GmbH" / "SA": dotted single-letter abbreviations are joined before punctuation is dropped.
_DOTTED = re.compile(r"(?<!\w)(?:\w\.){2,}")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")
# Words that carry little identity in names ("Plant", "Warehouse"); only used for tokens, never for exact keys.
GENERIC_TOKENS = frozenset(
    {
        "supplier", "supply", "supplies", "trading", "industries", "industrial", "international", "global",
        "services", "solutions", "systems", "logistics", "manufacturing", "plant", "factory", "warehouse",
        "depot", "site", "center", "centre", "distribution", "facility", "products", "materials",
    }
)  # fmt: skip


def normalize_name(value: str | None) -> str:
    """Casefold, NFKC, drop punctuation, collapse whitespace. '' for empty input."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).casefold()
    text = _DOTTED.sub(lambda m: m.group(0).replace(".", ""), text)
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def name_tokens(value: str | None, *, drop_legal: bool = True, drop_generic: bool = False) -> list[str]:
    toks = normalize_name(value).split()
    if drop_legal:
        toks = [t for t in toks if t not in LEGAL_FORMS]
    if drop_generic:
        toks = [t for t in toks if t not in GENERIC_TOKENS]
    return toks


def core_name(value: str | None) -> str:
    """Name without legal forms (e.g. 'Acme Components Ltd.' -> 'acme components')."""
    return " ".join(name_tokens(value))


def normalize_address(value: str | None) -> str:
    return normalize_name(value)


def normalize_identifier(value: str) -> str:
    """Identifiers are case-sensitive: only Unicode NFC and surrounding whitespace are normalised."""
    return unicodedata.normalize("NFC", value).strip()


def like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
