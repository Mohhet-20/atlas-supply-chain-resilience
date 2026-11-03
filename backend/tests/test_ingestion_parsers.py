from __future__ import annotations

import json

import pytest

from app.ingestion.export import neutralize, to_csv
from app.ingestion.fieldspecs import FieldSpec
from app.ingestion.normalize import coerce_field, parse_date, parse_number
from app.ingestion.parsers import json_depth_exceeds, parse_csv, parse_json
from app.models.enums import FacilityType


def csv_(text: str | bytes, enc="utf-8", delim=",", max_rows=100):
    data = text.encode(enc) if isinstance(text, str) else text
    return parse_csv(data, encoding=enc, delimiter=delim, max_rows=max_rows)


def test_csv_basic_and_original_line_numbers_with_multiline_cells():
    p = csv_('id,name\n1,"multi\nline"\n\n2,plain\n')
    assert [(r.index, r.line) for r in p.rows] == [(1, 2), (2, 5)]  # blank line skipped, numbering stays physical
    assert p.rows[0].values["name"] == "multi\nline"


def test_csv_bom_and_alternate_delimiters_and_encodings():
    assert csv_("﻿id;name\n1;a\n", enc="utf-8-sig", delim=";").columns == ["id", "name"]
    assert csv_("id\tname\n1\ta\n", delim="tab").rows[0].values == {"id": "1", "name": "a"}
    assert csv_("id|name\n1|a\n", delim="pipe").rows[0].values["name"] == "a"
    assert csv_("id,name\n1,Caf\xe9\n".encode("latin-1"), enc="latin-1").rows[0].values["name"] == "Caf\xe9"
    assert csv_("id,name\n1,a\n", enc="utf-16").rows[0].values["id"] == "1"


def test_csv_fatal_file_problems():
    for bad, needle in [
        (b"", "empty"),
        (b"id,id\n1,2\n", "Duplicate"),
        (b"id,name\n", "no data rows"),
        (b"id,name\n1,a\x00b\n", "Binary"),
        ("id\n\xff\n".encode("latin-1"), "not valid utf-8"),
    ]:
        p = parse_csv(bad, encoding="utf-8", delimiter=",", max_rows=10)
        assert any(needle.lower() in f.message.lower() for f in p.file_findings), (bad, p.file_findings)
    assert csv_("a\n1\n", delim="x").fatal
    assert parse_csv(b"a", encoding="nope", delimiter=",", max_rows=5).fatal


def test_csv_ragged_rows_are_row_errors_not_file_errors():
    p = csv_("a,b\n1,2\n1\n3,4,5\n6,7\n")
    assert [r.error is not None for r in p.rows] == [False, True, True, False]
    assert p.rows[1].error.rule_id == "DQ013" and p.rows[1].line == 3
    assert not p.fatal


def test_csv_row_and_column_limits():
    assert csv_("a\n" + "1\n" * 11, max_rows=10).fatal
    assert csv_(",".join(f"c{i}" for i in range(101)) + "\n" + ",".join("1" for _ in range(101)) + "\n").fatal


def test_json_shapes_and_flattening():
    assert len(parse_json(b'{"a": 1}', encoding="utf-8", max_rows=5).rows) == 1
    assert len(parse_json(b'[{"a": 1}, {"a": 2}]', encoding="utf-8", max_rows=5).rows) == 2
    p = parse_json(
        json.dumps({"records": [{"x": {"y": {"z": 1}}, "t": [1, 2]}]}).encode(), encoding="utf-8", max_rows=5
    )
    assert p.rows[0].values == {"x.y.z": 1, "t": [1, 2]}
    p = parse_json(b'[1, {"a": 1}]', encoding="utf-8", max_rows=5)
    assert p.rows[0].error and p.rows[0].error.rule_id == "DQ013" and not p.rows[1].error


@pytest.mark.parametrize(
    "payload", [b"[", b"NaN", b'{"a": Infinity}', b'{"a":1,"a":2}', b"42", b'"str"', b"[]", b"{bad"]
)
def test_json_rejections(payload):
    assert parse_json(payload, encoding="utf-8", max_rows=5).fatal


def test_json_depth_bomb_is_rejected_without_recursion():
    bomb = ("[" * 100_000 + "]" * 100_000).encode()
    p = parse_json(bomb, encoding="utf-8", max_rows=5)
    assert p.fatal and "nested deeper" in p.file_findings[0].message
    assert not json_depth_exceeds(
        '{"a": "[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[["}', 5
    )  # brackets inside strings ignored
    assert json_depth_exceeds("[" * 40, 32)


def test_json_row_limit():
    assert parse_json(b"[" + b",".join([b"{}"] * 11) + b"]", encoding="utf-8", max_rows=10).fatal


def test_number_parsing_is_strict_and_zero_is_not_unknown():
    assert parse_number("0") == 0.0 and parse_number("-0.5") == -0.5 and parse_number("1e3") == 1000.0
    for bad in ["1,000", "1 000", "abc", "nan", "inf", "-", "", "1.2.3", "0x10", "١٢٣"]:
        with pytest.raises(ValueError):
            parse_number(bad)


def test_date_parsing_never_guesses():
    assert str(parse_date("2026-02-28", "%Y-%m-%d")) == "2026-02-28"
    for bad in ["2026-02-30", "28/02/2026", "2026-02-28T10:00:00Z", "tomorrow"]:
        with pytest.raises(ValueError):
            parse_date(bad, "%Y-%m-%d")
    assert str(parse_date("03/04/2026", "%d/%m/%Y")) == "2026-04-03"
    with pytest.raises(ValueError):
        parse_date("03/04/2026", "%x")


def test_coerce_field_semantics():
    num = FieldSpec("cap", "number", nonneg=True)
    assert coerce_field(num, "0", "%Y-%m-%d")[0] == 0.0
    assert coerce_field(num, None, "%Y-%m-%d") == (None, [])  # unknown stays unknown
    assert coerce_field(num, "-1", "%Y-%m-%d")[1][0].rule_id == "DQ006"
    assert coerce_field(num, "x", "%Y-%m-%d")[1][0].rule_id == "DQ003"
    geo = FieldSpec("lat", "number", minimum=-90, maximum=90)
    assert coerce_field(geo, "91", "%Y-%m-%d")[1][0].rule_id == "DQ007"
    en = FieldSpec("t", "enum", enum=FacilityType)
    assert coerce_field(en, "cross dock", "%Y-%m-%d")[0] is FacilityType.CROSS_DOCK
    assert coerce_field(en, "bridge", "%Y-%m-%d")[1][0].rule_id == "DQ005"
    ident = FieldSpec("external_id", "id", required=True)
    for bad in ["a b", "x:y", "../etc/passwd", "'; DROP TABLE suppliers;--", "é1", "a" * 65, "-x"]:
        assert coerce_field(ident, bad, "%Y-%m-%d")[1][0].rule_id == "DQ002", bad
    for good in ["A1", "ab.c-d_e/f#1", "9", "S" * 64]:
        assert coerce_field(ident, good, "%Y-%m-%d")[1] == []
    assert coerce_field(ident, None, "%Y-%m-%d")[1][0].rule_id == "DQ001"


def test_formula_neutralization():
    for risky in ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"]:
        assert neutralize(risky).startswith("'")
    assert neutralize("safe") == "safe" and neutralize(None) == "" and neutralize(5) == "5"
    out = to_csv(["=h"], [["=a", "b,c", 'q"d']])
    assert out.startswith("'=h") and "'=a" in out and '"b,c"' in out and '"q""d"' in out
