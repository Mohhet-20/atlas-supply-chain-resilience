"""Rules for canonical suppliers, facilities and products.

Thresholds and the required/recommended field lists come from the existing validated ``QualityConfig``; nothing is
duplicated here. A rule returns one ``Check`` per applicable field: passed checks are part of the score denominator.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.enums import (
    CandidateStatus,
    CandidateTier,
    EntityKind,
    EntityStatus,
    LocationStatus,
    QualityDimension,
    Severity,
)
from app.quality.context import Context
from app.quality.framework import ENTITY_KINDS, Check, RuleSpec, Subject, register, safe_value

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UOM = re.compile(r"^[A-Za-z][A-Za-z0-9 /._%-]{0,31}$")
_EXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/#]{0,63}\Z")
S, F, P = EntityKind.SUPPLIER, EntityKind.FACILITY, EntityKind.PRODUCT
TEXT_FIELDS = ("name", "legal_name", "address", "city", "country_or_region", "category")


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _rules(sub: Subject, ctx: Context):
    return ctx.qcfg.entities[sub.kind.value]


# ------------------------------------------------------------------------------------------------ completeness


def required_fields(sub: Subject, ctx: Context) -> list[Check]:
    out = []
    for f in _rules(sub, ctx).fields:
        if f.required:
            missing = _empty(getattr(sub.obj, f.name, None))
            out.append(
                Check(
                    f.name, not missing, f"Required field '{f.name}' is empty.", {"field": f.name, "state": "missing"}
                )
            )
    return out


def recommended_fields(sub: Subject, ctx: Context) -> list[Check]:
    return [
        Check(
            n,
            not _empty(getattr(sub.obj, n, None)),
            f"Recommended field '{n}' is unknown.",
            {"field": n, "state": "unknown"},
            Severity.INFO,
        )
        for n in _rules(sub, ctx).recommended
    ]


# ---------------------------------------------------------------------------------------------------- validity


def text_clean(sub: Subject, ctx: Context) -> list[Check]:
    out = []
    for fname in TEXT_FIELDS:
        v = getattr(sub.obj, fname, None)
        if isinstance(v, str) and v:
            bad = v != v.strip() or bool(_CONTROL.search(v))
            out.append(
                Check(
                    fname,
                    not bad,
                    f"'{fname}' has leading/trailing whitespace or control characters.",
                    {"field": fname, "length": len(v)},
                )
            )
    return out


def country_not_numeric(sub: Subject, ctx: Context) -> list[Check]:
    v = getattr(sub.obj, "country_or_region", None)
    if not v:
        return []
    return [
        Check(
            "country_or_region",
            not v.strip().isdigit(),
            "Country/region is purely numeric.",
            {"field": "country_or_region", "observed": safe_value("country_or_region", v)},
        )
    ]


def external_id_policy(sub: Subject, ctx: Context) -> list[Check]:
    v = getattr(sub.obj, "external_id", None)
    if not v:
        return []
    return [
        Check(
            "external_id",
            bool(_EXT.match(v)),
            "External identifier violates the identifier policy.",
            {"field": "external_id", "observed": safe_value("external_id", v)},
        )
    ]


def coordinates_in_range(sub: Subject, ctx: Context) -> list[Check]:
    f = sub.obj
    if f.latitude is None and f.longitude is None:
        return []  # unknown stays unknown; never treated as zero
    if (f.latitude is None) != (f.longitude is None):
        return [
            Check(
                "latitude",
                False,
                "Only one of latitude/longitude is set.",
                {"field": "latitude", "state": "incomplete_pair"},
            )
        ]
    ok = -90 <= f.latitude <= 90 and -180 <= f.longitude <= 180
    return [
        Check(
            "latitude", ok, "Coordinates are outside the valid range.", {"field": "latitude", "state": "out_of_range"}
        )
    ]


def coordinates_not_placeholder(sub: Subject, ctx: Context) -> list[Check]:
    f = sub.obj
    if f.latitude is None or f.longitude is None:
        return []
    return [
        Check(
            "latitude",
            not (f.latitude == 0 and f.longitude == 0),
            "Coordinates (0, 0) are a placeholder, not a site.",
            {"field": "latitude", "observed": "0,0"},
        )
    ]


def capacity_valid(sub: Subject, ctx: Context) -> list[Check]:
    f = sub.obj
    if f.capacity_value is None:
        return []
    ok = f.capacity_value >= 0 and bool(f.capacity_unit)
    return [
        Check(
            "capacity_value",
            ok,
            "Capacity must be non-negative and carry a unit.",
            {"field": "capacity_value", "has_unit": bool(f.capacity_unit)},
        )
    ]


def uom_label_valid(sub: Subject, ctx: Context) -> list[Check]:
    v = sub.obj.unit_of_measure
    if not v:
        return []
    return [
        Check(
            "unit_of_measure",
            bool(_UOM.match(v)),
            "Unit of measure is not a plain unit label.",
            {"field": "unit_of_measure", "observed": safe_value("unit_of_measure", v)},
        )
    ]


# ------------------------------------------------------------------------------------------------ consistency


def location_status_consistent(sub: Subject, ctx: Context) -> list[Check]:
    f = sub.obj
    has = f.latitude is not None
    ok = (f.location_status is LocationStatus.UNKNOWN) == (not has)
    return [
        Check(
            "location_status",
            ok,
            "location_status disagrees with whether coordinates exist.",
            {"field": "location_status", "has_coordinates": has},
        )
    ]


def facility_owner_in_service(sub: Subject, ctx: Context) -> list[Check]:
    f = sub.obj
    if not f.owner_supplier_id:
        return []
    owner = ctx.supplier_info.get(f.owner_supplier_id)
    bad = f.status is EntityStatus.ACTIVE and owner is not None and owner[0] is EntityStatus.INACTIVE
    return [
        Check(
            "owner_supplier_id",
            not bad,
            "Active facility is owned by an inactive supplier.",
            {"field": "owner_supplier_id"},
        )
    ]


def inactive_supplier_relationships(sub: Subject, ctx: Context) -> list[Check]:
    s = sub.obj
    bad = s.status is EntityStatus.INACTIVE and ctx.supplier_rel_count.get(s.id, 0) > 0
    return [
        Check(
            "status",
            not bad,
            "Inactive supplier still has supply relationships.",
            {"field": "status", "relationships": ctx.supplier_rel_count.get(s.id, 0)},
        )
    ]


def retired_product_links(sub: Subject, ctx: Context) -> list[Check]:
    p = sub.obj
    bad = p.retired_at is not None and ctx.product_link_count.get(p.id, 0) > 0
    return [
        Check(
            "retired_at",
            not bad,
            "Retired product still has product links.",
            {"field": "retired_at", "links": ctx.product_link_count.get(p.id, 0)},
        )
    ]


def retired_product_status(sub: Subject, ctx: Context) -> list[Check]:
    p = sub.obj
    bad = p.retired_at is not None and p.status is EntityStatus.ACTIVE
    return [Check("status", not bad, "Retired product is marked ACTIVE.", {"field": "status"})]


def product_link_units(sub: Subject, ctx: Context) -> list[Check]:
    p = sub.obj
    if not p.unit_of_measure:
        return []
    mine = p.unit_of_measure.strip().upper()
    others = sorted(u for u in ctx.product_link_units.get(p.id, set()) if u.strip().upper() != mine)
    return [
        Check(
            "unit_of_measure",
            not others,
            "Product links use a different unit than the product's unit of measure.",
            {
                "field": "unit_of_measure",
                "product_unit": safe_value(None, p.unit_of_measure),
                "link_units": [safe_value(None, u, 32) for u in others[:5]],
            },
        )
    ]


# ------------------------------------------------------------------------------------------------- uniqueness


def duplicate_candidates(sub: Subject, ctx: Context) -> list[Check]:
    rtype = ctx.rtype_of(sub.kind)
    cands = ctx.candidates.get((rtype, sub.id), [])
    if not cands:
        return [Check(None, True)]
    out = []
    for c in cands:
        other = c.right_id if c.left_id == sub.id else c.left_id
        sev = (
            Severity.ERROR
            if c.status is CandidateStatus.CONFIRMED_DUPLICATE or c.tier is CandidateTier.STRONG
            else Severity.WARNING
        )
        out.append(
            Check(
                f"candidate:{other}",
                False,
                f"{c.tier.value.title()} duplicate candidate with record {other} ({c.status.value}).",
                {"other_id": other, "candidate_id": c.id, "tier": c.tier.value, "status": c.status.value},
                sev,
            )
        )
    return out


def duplicate_location(sub: Subject, ctx: Context) -> list[Check]:
    f = sub.obj
    if f.latitude is None or f.longitude is None or (f.latitude == 0 and f.longitude == 0):
        return []
    others = sorted(set(ctx.location_dupes.get(f.id, [])))
    if not others:
        return [Check(None, True)]
    return [
        Check(
            f"location:{o}",
            False,
            f"Another live facility ({o}) has the same coordinates (to 4 decimals).",
            {"other_id": o},
        )
        for o in others[:5]
    ]


def shared_authoritative_identifier(sub: Subject, ctx: Context) -> list[Check]:
    rtype = ctx.rtype_of(sub.kind)
    hits = ctx.authid_dupes.get((rtype, sub.id), [])
    if not hits:
        return [Check(None, True)]
    return [
        Check(
            f"identifier:{scheme}:{other}",
            False,
            f"Authoritative {scheme} identifier is also held by record {other}.",
            {"scheme": scheme, "other_id": other},
        )
        for scheme, _v, other in hits[:5]
    ]


# -------------------------------------------------------------------------------------------------- freshness


def freshness(sub: Subject, ctx: Context) -> list[Check]:
    e = sub.obj
    updated = getattr(e, "updated_at", None) or e.created_at
    age = max(0.0, (ctx.now - updated).total_seconds() / 86400)
    cfg = ctx.qcfg
    sev = Severity.ERROR if age >= cfg.stale_days else Severity.WARNING
    return [
        Check(
            None,
            age <= cfg.fresh_days,
            f"Last updated {int(age)} days ago (policy: within {cfg.fresh_days}).",
            {"age_days": int(age), "fresh_days": cfg.fresh_days, "stale_days": cfg.stale_days},
            sev,
        )
    ]


# -------------------------------------------------------------------------------------------------- provenance


def source_reference(sub: Subject, ctx: Context) -> list[Check]:
    e = sub.obj
    return [
        Check(
            "source",
            e.source_id is not None and e.external_id is not None,
            "No source system identifier is recorded.",
            {"field": "source"},
        )
    ]


def origin_traceable(sub: Subject, ctx: Context) -> list[Check]:
    rtype = ctx.rtype_of(sub.kind)
    has_source = sub.obj.source_id is not None and sub.obj.external_id is not None
    ok = ctx.lineage.get((sub.kind, sub.id), 0) > 0 or (rtype, sub.id) in ctx.created_audit or has_source
    return [
        Check(
            "lineage",
            ok,
            "No source reference, ingestion lineage or creation audit event explains where this record came from.",
            {"lineage_rows": ctx.lineage.get((sub.kind, sub.id), 0), "has_source_reference": has_source},
        )
    ]


# ------------------------------------------------------------------------------------- referential integrity


def references_live(sub: Subject, ctx: Context) -> list[Check]:
    out = []
    for attr in ("parent_supplier_id", "owner_supplier_id", "manufacturer_supplier_id"):
        v = getattr(sub.obj, attr, None)
        if v:
            info = ctx.supplier_info.get(v)
            merged = info is not None and info[1] is not None
            out.append(
                Check(
                    attr,
                    not merged,
                    f"{attr} points at a supplier that was merged away.",
                    {"field": attr, "target_id": v},
                )
            )
    return out


_D = QualityDimension
_R = register
_R(
    RuleSpec(
        "QR101",
        1,
        "required_field_present",
        "Every field the quality configuration marks as required has a value.",
        _D.COMPLETENESS,
        Severity.ERROR,
        ENTITY_KINDS,
        required_fields,
        "Obtain the value from the source system or the owning team. Never guess it.",
        params=("entities.*.fields",),
    )
)
_R(
    RuleSpec(
        "QR102",
        1,
        "recommended_field_known",
        "Recommended (optional) fields that are still unknown. Informational; never lowers a score.",
        _D.COMPLETENESS,
        Severity.INFO,
        ENTITY_KINDS,
        recommended_fields,
        "Add the value if it is known. Optional attributes are not required.",
        default_enabled=False,
        params=("entities.*.recommended",),
    )
)
_R(
    RuleSpec(
        "QR111",
        1,
        "text_fields_clean",
        "Text fields have no leading/trailing whitespace or control characters.",
        _D.VALIDITY,
        Severity.WARNING,
        ENTITY_KINDS,
        text_clean,
        "Trim the value.",
        fix_action="TRIM_TEXT",
    )
)
_R(
    RuleSpec(
        "QR112",
        1,
        "country_not_numeric",
        "Country/region is not purely numeric.",
        _D.VALIDITY,
        Severity.WARNING,
        ENTITY_KINDS,
        country_not_numeric,
        "Use a country or region name.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR113",
        1,
        "external_id_policy",
        "The source identifier follows the identifier policy.",
        _D.VALIDITY,
        Severity.ERROR,
        ENTITY_KINDS,
        external_id_policy,
        "Use letters or digits followed by . _ - / #.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR114",
        1,
        "coordinates_in_range",
        "Coordinates come as a complete pair inside the valid range. Unknown coordinates are not an error.",
        _D.VALIDITY,
        Severity.ERROR,
        (F,),
        coordinates_in_range,
        "Correct the coordinates from a trusted source, or clear them if unknown.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR115",
        1,
        "coordinates_not_placeholder",
        "Coordinates are not the (0, 0) placeholder.",
        _D.VALIDITY,
        Severity.ERROR,
        (F,),
        coordinates_not_placeholder,
        "Clear the coordinates if the real location is unknown.",
        fix_action="CLEAR_PLACEHOLDER_COORDINATES",
    )
)
_R(
    RuleSpec(
        "QR116",
        1,
        "capacity_valid",
        "A stated capacity is non-negative and has a unit.",
        _D.VALIDITY,
        Severity.ERROR,
        (F,),
        capacity_valid,
        "Fix the capacity value or unit, or clear both.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR117",
        1,
        "uom_label_valid",
        "The unit of measure is a plain unit label.",
        _D.VALIDITY,
        Severity.WARNING,
        (P,),
        uom_label_valid,
        "Use a short unit label such as EA or KG.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR121",
        1,
        "location_status_consistent",
        "location_status agrees with whether coordinates exist.",
        _D.CONSISTENCY,
        Severity.WARNING,
        (F,),
        location_status_consistent,
        "Set location_status to UNKNOWN when there are no coordinates.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR122",
        1,
        "facility_owner_in_service",
        "An active facility is not owned by an inactive supplier.",
        _D.CONSISTENCY,
        Severity.WARNING,
        (F,),
        facility_owner_in_service,
        "Review the facility or owner status.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR123",
        1,
        "inactive_supplier_has_no_relationships",
        "An inactive supplier has no live supply relationships.",
        _D.CONSISTENCY,
        Severity.WARNING,
        (S,),
        inactive_supplier_relationships,
        "Review the supplier status or deactivate its relationships.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR124",
        1,
        "retired_product_has_no_links",
        "A retired product has no product links.",
        _D.CONSISTENCY,
        Severity.WARNING,
        (P,),
        retired_product_links,
        "Review the links or reactivate the product.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR125",
        1,
        "retired_product_not_active",
        "A retired product is not marked ACTIVE.",
        _D.CONSISTENCY,
        Severity.WARNING,
        (P,),
        retired_product_status,
        "Set the status to INACTIVE for retired products.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR126",
        1,
        "product_link_units_consistent",
        "Product link quantity units match the product's unit of measure.",
        _D.CONSISTENCY,
        Severity.WARNING,
        (P,),
        product_link_units,
        "Confirm the unit conversion with the data owner; units are never converted automatically.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR131",
        1,
        "no_open_duplicate_candidate",
        "No open or confirmed duplicate candidate exists for the record.",
        _D.UNIQUENESS,
        Severity.WARNING,
        ENTITY_KINDS,
        duplicate_candidates,
        "Review the candidate in the duplicate-review workspace and confirm, reject or merge it.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR132",
        1,
        "no_duplicate_location",
        "No other live facility has the same coordinates (to 4 decimals).",
        _D.UNIQUENESS,
        Severity.WARNING,
        (F,),
        duplicate_location,
        "Check whether both records describe the same site.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR133",
        1,
        "authoritative_identifier_unique",
        "Authoritative identifiers (for example LEI) are held by one record only.",
        _D.UNIQUENESS,
        Severity.ERROR,
        ENTITY_KINDS,
        shared_authoritative_identifier,
        "Review both records; one of them is probably a duplicate.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR141",
        1,
        "record_fresh",
        "The record was updated within the configured freshness window.",
        _D.FRESHNESS,
        Severity.WARNING,
        ENTITY_KINDS,
        freshness,
        "Confirm the record with its source and update it.",
        requires_review=True,
        params=("fresh_days", "stale_days"),
    )
)
_R(
    RuleSpec(
        "QR151",
        1,
        "source_reference_present",
        "A source system identifier is recorded. Manual records legitimately have none, so this is informational.",
        _D.PROVENANCE_COMPLETENESS,
        Severity.INFO,
        ENTITY_KINDS,
        source_reference,
        "Attach the source system's identifier if one exists.",
    )
)
_R(
    RuleSpec(
        "QR152",
        1,
        "origin_traceable",
        "A source reference, ingestion lineage or a creation audit event explains the record's origin.",
        _D.PROVENANCE_COMPLETENESS,
        Severity.WARNING,
        ENTITY_KINDS,
        origin_traceable,
        "Re-ingest from the source, or record how the record was created.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR161",
        1,
        "references_not_merged_away",
        "Parent, owner and manufacturer references point at surviving records.",
        _D.REFERENTIAL_INTEGRITY,
        Severity.ERROR,
        ENTITY_KINDS,
        references_live,
        "Point the reference at the surviving (merged-into) record.",
    )
)
