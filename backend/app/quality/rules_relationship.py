"""Rules for supply relationships and product links (the graph edges).

Structural checks reuse the graph's own vocabulary (``app.graph.rules``) and lifecycle loader, and mirror the
integrity diagnostics GRP001-GRP009 so the two cannot disagree. A PROPOSED relationship is *not* an error: its status
and confidence are reported separately (QR213, informational). An unknown optional attribute never fails a check.
"""

from __future__ import annotations

from app.graph.rules import RULES
from app.graph.store import EdgeRec
from app.models.enums import EdgeStatus, QualityDimension, Severity
from app.quality.context import Context
from app.quality.framework import EDGE_KINDS, Check, RuleSpec, Subject, register, safe_value


def _e(sub: Subject) -> EdgeRec:
    return sub.obj


def endpoints_exist(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    out = []
    for label, key in (("source", e.source), ("destination", e.destination)):
        missing = key[1] is None or key not in ctx.lifecycle
        out.append(
            Check(
                label,
                not missing,
                f"The {label} {key[0].value} {key[1]} does not exist.",
                {"endpoint": label, "type": key[0].value, "id": key[1]},
            )
        )
    return out


def endpoints_not_merged(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    out = []
    for label, key in (("source", e.source), ("destination", e.destination)):
        if key in ctx.lifecycle:
            out.append(
                Check(
                    label,
                    ctx.lifecycle[key] != "MERGED",
                    f"The {label} {key[0].value} {key[1]} was merged away.",
                    {"endpoint": label, "type": key[0].value, "id": key[1]},
                )
            )
    return out


def valid_combination(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    rule = RULES[e.type]
    ok = (e.source[0], e.destination[0]) == (rule.source, rule.destination)
    return [
        Check(
            None,
            ok,
            f"{e.type.value} cannot connect {e.source[0].value} to {e.destination[0].value}.",
            {"type": e.type.value, "source_type": e.source[0].value, "destination_type": e.destination[0].value},
        )
    ]


def no_self_link(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    return [
        Check(
            None,
            e.source != e.destination,
            "The relationship connects a record to itself.",
            {"type": e.source[0].value, "id": e.source[1]},
        )
    ]


def unique_relationship(sub: Subject, ctx: Context) -> list[Check]:
    return [
        Check(
            None,
            sub.ref not in ctx.dup_edge_refs,
            "Another relationship has the same type, source and destination.",
            {"ref": sub.ref},
        )
    ]


def acyclic(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    if RULES[e.type].cycles != "impossible":
        return []  # cycle-tolerant types (TRANSFERS_TO) may legitimately loop: not a quality problem
    return [
        Check(
            None,
            sub.ref not in ctx.cycle_refs,
            "The relationship lies on a cycle, which is not allowed for this type.",
            {"ref": sub.ref, "type": e.type.value},
        )
    ]


def effective_dates(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    if e.effective_from is None or e.effective_to is None:
        return []
    return [
        Check(
            "effective_to",
            e.effective_to >= e.effective_from,
            "The effective period ends before it starts.",
            {"from": safe_value(None, e.effective_from), "to": safe_value(None, e.effective_to)},
        )
    ]


def attribute_ranges(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    out = []
    if e.lead_time_days is not None:
        out.append(
            Check(
                "lead_time_days",
                e.lead_time_days >= 0,
                "Lead time must not be negative.",
                {"observed": e.lead_time_days},
            )
        )
    if e.capacity_value is not None:
        out.append(
            Check(
                "capacity_value",
                e.capacity_value >= 0 and bool(e.capacity_unit),
                "Capacity must be non-negative and carry a unit.",
                {"observed": e.capacity_value, "has_unit": bool(e.capacity_unit)},
            )
        )
    if e.confidence is not None:
        out.append(
            Check(
                "confidence", 0 <= e.confidence <= 1, "Confidence must be between 0 and 1.", {"observed": e.confidence}
            )
        )
    if e.supply_share_pct is not None:
        out.append(
            Check(
                "supply_share_pct",
                0 <= e.supply_share_pct <= 100,
                "Supply share must be between 0 and 100 percent.",
                {"observed": e.supply_share_pct},
            )
        )
    return out


def provenance_recorded(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    ok = bool(e.source_namespace or e.created_by or sub.ref in ctx.edge_evidence)
    return [
        Check(
            "provenance",
            ok,
            "Nothing records where this relationship came from (no source, creator, lineage or history).",
            {"provenance": e.provenance},
        )
    ]


def status_provenance_consistent(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    out = [
        Check(
            "provenance",
            not (e.status is EdgeStatus.CONFIRMED and e.provenance in ("MODEL_ESTIMATE", "SIMULATED")),
            f"Relationship is CONFIRMED but its provenance is {e.provenance}, which is not an observed fact.",
            {"status": e.status.value, "provenance": e.provenance},
        )
    ]
    if e.status in (EdgeStatus.REJECTED, EdgeStatus.INACTIVE):
        out.append(
            Check(
                "status_reason",
                bool(e.status_reason),
                f"Relationship is {e.status.value} without a recorded reason.",
                {"status": e.status.value},
            )
        )
    return out


def endpoints_in_service(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    if not (e.status is EdgeStatus.CONFIRMED and e.effective_on(ctx.today)):
        return []
    out = []
    for label, key in (("source", e.source), ("destination", e.destination)):
        state = ctx.lifecycle.get(key)
        if state is not None:
            out.append(
                Check(
                    label,
                    state not in ("INACTIVE", "ON_HOLD", "RETIRED"),
                    f"The relationship is active but its {label} record is {state}.",
                    {"endpoint": label, "lifecycle": state},
                )
            )
    return out


def confirmed_not_expired(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    if e.status is not EdgeStatus.CONFIRMED:
        return []
    expired = e.effective_to is not None and e.effective_to < ctx.today
    return [
        Check(
            "effective_to",
            not expired,
            "The relationship is CONFIRMED but its effective period has ended.",
            {"effective_to": safe_value(None, e.effective_to)},
        )
    ]


def verification_status(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    return [
        Check(
            "status",
            e.status is not EdgeStatus.PROPOSED,
            "The relationship is PROPOSED and awaiting review. This is a status, not a structural error.",
            {"status": e.status.value, "confidence": e.confidence},
            Severity.INFO,
        )
    ]


def relationship_fresh(sub: Subject, ctx: Context) -> list[Check]:
    e = _e(sub)
    if e.status is not EdgeStatus.CONFIRMED:
        return []
    updated = e.updated_at or e.created_at
    if updated is None:
        return []
    age = max(0.0, (ctx.now - updated).total_seconds() / 86400)
    return [
        Check(
            None,
            age <= ctx.qcfg.fresh_days,
            f"A confirmed relationship was last updated {int(age)} days ago (policy: within {ctx.qcfg.fresh_days}).",
            {"age_days": int(age), "fresh_days": ctx.qcfg.fresh_days},
            Severity.ERROR if age >= ctx.qcfg.stale_days else Severity.WARNING,
        )
    ]


_D = QualityDimension
_R = register
_R(
    RuleSpec(
        "QR201",
        1,
        "endpoints_exist",
        "Both endpoints exist in the registry (mirrors GRP001).",
        _D.REFERENTIAL_INTEGRITY,
        Severity.ERROR,
        EDGE_KINDS,
        endpoints_exist,
        "Restore the registry record or reject the relationship; history is never deleted.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR202",
        1,
        "endpoints_not_merged_away",
        "Neither endpoint was merged away (mirrors GRP006).",
        _D.REFERENTIAL_INTEGRITY,
        Severity.ERROR,
        EDGE_KINDS,
        endpoints_not_merged,
        "Re-point the relationship to the surviving record (re-run or compensate the merge).",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR203",
        1,
        "valid_type_endpoint_combination",
        "The relationship type connects the allowed entity types (mirrors GRP002).",
        _D.RELATIONSHIP_QUALITY,
        Severity.ERROR,
        EDGE_KINDS,
        valid_combination,
        "Reject it and create a relationship of the correct type.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR204",
        1,
        "no_self_link",
        "A relationship does not connect a record to itself (mirrors GRP004).",
        _D.RELATIONSHIP_QUALITY,
        Severity.ERROR,
        EDGE_KINDS,
        no_self_link,
        "Reject the relationship.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR205",
        1,
        "relationship_unique",
        "One relationship per type, source and destination (mirrors GRP003).",
        _D.UNIQUENESS,
        Severity.ERROR,
        EDGE_KINDS,
        unique_relationship,
        "Keep one, reject the others and record why.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR206",
        1,
        "no_cycle_in_acyclic_type",
        "Types defined as acyclic contain no directed cycle. Cycle-tolerant types (TRANSFERS_TO) are not checked.",
        _D.RELATIONSHIP_QUALITY,
        Severity.ERROR,
        EDGE_KINDS,
        acyclic,
        "Review the relationships on the cycle; one of them is wrong.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR207",
        1,
        "effective_dates_valid",
        "The effective period does not end before it starts (mirrors GRP008).",
        _D.VALIDITY,
        Severity.ERROR,
        EDGE_KINDS,
        effective_dates,
        "Correct the effective dates through the relationship update endpoint.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR208",
        1,
        "attribute_ranges_valid",
        "Stated lead time, capacity, confidence and supply share are inside their valid ranges. Unknown values are not errors.",
        _D.VALIDITY,
        Severity.ERROR,
        EDGE_KINDS,
        attribute_ranges,
        "Correct the value or clear it if unknown. Never invent one.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR209",
        1,
        "relationship_provenance_recorded",
        "Something records where the relationship came from.",
        _D.PROVENANCE_COMPLETENESS,
        Severity.WARNING,
        EDGE_KINDS,
        provenance_recorded,
        "Re-ingest from the source or add the evidence in the next status change.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR210",
        1,
        "status_provenance_consistent",
        "Status, provenance and recorded reason do not contradict each other (mirrors GRP009).",
        _D.RELATIONSHIP_QUALITY,
        Severity.WARNING,
        EDGE_KINDS,
        status_provenance_consistent,
        "Have a reviewer re-check the evidence, or change the status with a reason.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR211",
        1,
        "active_endpoints_in_service",
        "An active relationship does not sit on an inactive, on-hold or retired record (mirrors GRP007).",
        _D.CONSISTENCY,
        Severity.WARNING,
        EDGE_KINDS,
        endpoints_in_service,
        "Deactivate the relationship, or reactivate the record.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR212",
        1,
        "confirmed_not_expired",
        "A CONFIRMED relationship's effective period has not ended.",
        _D.RELATIONSHIP_QUALITY,
        Severity.WARNING,
        EDGE_KINDS,
        confirmed_not_expired,
        "Deactivate the relationship or extend its dates with a reason.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR213",
        1,
        "relationship_verified",
        "Reports PROPOSED relationships separately from structural errors. Informational; never lowers a score.",
        _D.RELATIONSHIP_QUALITY,
        Severity.INFO,
        EDGE_KINDS,
        verification_status,
        "A reviewer confirms or rejects the proposal in the Network Explorer.",
        requires_review=True,
    )
)
_R(
    RuleSpec(
        "QR214",
        1,
        "relationship_fresh",
        "A confirmed relationship was updated within the configured freshness window.",
        _D.FRESHNESS,
        Severity.WARNING,
        EDGE_KINDS,
        relationship_fresh,
        "Re-verify the relationship with its source.",
        requires_review=True,
        params=("fresh_days", "stale_days"),
    )
)
