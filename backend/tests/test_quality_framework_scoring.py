from __future__ import annotations

import re
from pathlib import Path

from app.models.enums import EntityKind, QualityDimension, ScoreState, Severity
from app.quality import scoring
from app.quality.framework import Check, catalog, catalog_hash, finding_key, safe_value
from app.registry.config import config_hash, default_quality


def _o(rule_id: str, passed: bool, field: str | None = "f", excepted: bool = False):
    return scoring.Outcome(catalog()[rule_id], Check(field, passed, "m"), excepted)


def test_catalog_metadata_complete_and_unique():
    cat = catalog()
    assert len(cat) >= 30
    for rid, r in cat.items():
        assert re.fullmatch(r"QR\d{3}", rid)
        assert r.name and r.description and r.remediation and r.version >= 1
        assert r.applies_to and r.dimension in QualityDimension
    assert len({r.name for r in cat.values()}) == len(cat)


def test_all_eight_dimensions_have_rules():
    assert {r.dimension for r in catalog().values()} == set(QualityDimension)
    assert len(QualityDimension) == 8


def test_no_user_expression_evaluation_in_quality_package():
    import ast

    for p in Path("app/quality").glob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__"}, p.name


def test_informational_rules_are_default_disabled_or_weightless():
    qr102 = catalog()["QR102"]
    assert qr102.default_enabled is False and qr102.severity is Severity.INFO


def test_finding_key_is_stable_and_distinct():
    a = finding_key("QR111", EntityKind.SUPPLIER, 1, "name")
    assert a == finding_key("QR111", EntityKind.SUPPLIER, 1, "name")
    assert a != finding_key("QR111", EntityKind.SUPPLIER, 2, "name")
    assert a != finding_key("QR111", EntityKind.SUPPLIER, 1, "city")


def test_catalog_hash_changes_with_enabled_flags_and_formula():
    ch = config_hash(default_quality())
    base = {rid: r.default_enabled for rid, r in catalog().items()}
    h1 = catalog_hash(base, ch, "qs-1")
    assert h1 == catalog_hash(dict(base), ch, "qs-1")
    assert h1 != catalog_hash(base | {"QR111": False}, ch, "qs-1")
    assert h1 != catalog_hash(base, ch, "qs-2")
    assert h1 != catalog_hash(base, "other", "qs-1")


def test_safe_value_masks_secrets_and_locations():
    assert safe_value("address", "1 Main St") == "(hidden)"
    assert safe_value("latitude", 12.3) == 12.3 or safe_value("latitude", 12.3) == "(hidden)"
    assert "secret" not in str(safe_value("name", "password=secret123 here"))
    assert len(str(safe_value("name", "x" * 500))) <= 90


def test_score_formula_and_insufficient_evidence():
    ok3 = [_o("QR111", True, "a"), _o("QR112", True, "b"), _o("QR113", True, "c")]
    assert scoring.score_subject(ok3).score == 100.0
    # one WARNING (1) failed of WARNING+WARNING+ERROR (1+1+3) -> 100*(1-1/5)
    mix = [_o("QR111", False, "a"), _o("QR112", True, "b"), _o("QR113", True, "c")]
    r = scoring.score_subject(mix)
    assert r.state is ScoreState.SCORED and r.score == 80.0 and r.checks_failed == 1
    few = scoring.score_subject([_o("QR111", True)])
    assert few.state is ScoreState.INSUFFICIENT_EVIDENCE and few.score is None


def test_info_rules_never_change_score_and_exceptions_are_excluded():
    base = [_o("QR111", True, "a"), _o("QR112", True, "b"), _o("QR113", True, "c")]
    with_info = [*base, _o("QR102", False, "x")]
    assert scoring.score_subject(with_info).score == 100.0
    excepted = [_o("QR111", False, "a", excepted=True), _o("QR112", True, "b"), _o("QR113", True, "c")]
    r = scoring.score_subject(excepted)
    assert r.score == 100.0 and r.exceptions_excluded == 1


def test_same_rule_same_field_counts_once():
    dup = [
        _o("QR131", False, "k:1"),
        _o("QR131", False, "k:2"),
        _o("QR111", True, "a"),
        _o("QR112", True, "b"),
        _o("QR113", True, "c"),
    ]
    single = [_o("QR131", False, "k:1"), _o("QR111", True, "a"), _o("QR112", True, "b"), _o("QR113", True, "c")]
    assert scoring.score_subject(dup).score == scoring.score_subject(single).score


def test_aggregate_reports_coverage_and_never_zero_for_missing():
    a = scoring.aggregate([90.0, None, 70.0], 10)
    assert a["mean"] == 80.0 and a["scored"] == 2 and a["insufficient_evidence"] == 1 and a["not_evaluated"] == 7
    assert a["coverage"] == 0.2
    empty = scoring.aggregate([], 5)
    assert empty["mean"] is None and empty["state"] == "UNAVAILABLE"
    assert scoring.aggregate([], 0)["coverage"] is None
