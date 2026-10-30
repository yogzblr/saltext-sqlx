"""Unit tests for saltext.sqlx.lib.serialize (TEST_PLAN section 3, SPEC section 9)."""

import datetime as dt
import decimal
import enum
import json
import uuid

import pytest

from saltext.sqlx.lib import serialize
from saltext.sqlx.lib.serialize import build_result
from saltext.sqlx.lib.serialize import serialize_row
from saltext.sqlx.lib.serialize import serialize_value


def strict(obj):
    """Dump with strict JSON and return the decoded round trip."""
    return json.loads(json.dumps(obj, allow_nan=False))


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        (True, True),
        (False, False),
        (0, 0),
        (-7, -7),
        (1.5, 1.5),
        ("", ""),
        ("héllo ✓", "héllo ✓"),
    ],
)
def test_passthrough_scalars(value, expected):
    out = serialize_value(value)
    assert out == expected
    assert isinstance(out, type(expected))
    assert strict(out) == expected


def test_decimal_is_string_without_float_rounding():
    d = decimal.Decimal("12.50")
    assert serialize_value(d) == "12.50"
    big = decimal.Decimal("12345678901234567890.123456789012345678")
    assert serialize_value(big) == "12345678901234567890.123456789012345678"
    assert serialize_value(decimal.Decimal("0.1")) == "0.1"


@pytest.mark.parametrize(
    "value,expected",
    [
        (decimal.Decimal("NaN"), "NaN"),
        (decimal.Decimal("Infinity"), "Infinity"),
        (decimal.Decimal("-Infinity"), "-Infinity"),
    ],
)
def test_decimal_special_values(value, expected):
    assert serialize_value(value) == expected


def test_datetime_date_time_iso():
    assert serialize_value(dt.datetime(2026, 10, 29, 12, 30, 5)) == "2026-10-29T12:30:05"
    aware = dt.datetime(2026, 10, 29, 12, 30, 5, 123456, tzinfo=dt.timezone.utc)
    assert serialize_value(aware) == "2026-10-29T12:30:05.123456+00:00"
    assert serialize_value(dt.date(2026, 10, 29)) == "2026-10-29"
    assert serialize_value(dt.time(1, 2, 3)) == "01:02:03"
    assert serialize_value(dt.time(1, 2, 3, tzinfo=dt.timezone.utc)) == "01:02:03+00:00"


def test_datetime_is_not_treated_as_date():
    # datetime subclasses date; the full timestamp must survive.
    assert "T" in serialize_value(dt.datetime(2020, 1, 1, 0, 0, 0))


def test_timedelta_string():
    assert serialize_value(dt.timedelta(days=1, hours=2)) == "1 day, 2:00:00"
    assert serialize_value(dt.timedelta(seconds=90)) == "0:01:30"


def test_uuid_string():
    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert serialize_value(u) == "12345678-1234-5678-1234-567812345678"


@pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview])
def test_bytes_like_base64(wrap):
    raw = b"\x00\xffabc"
    assert serialize_value(wrap(raw)) == {"$bytes_b64": "AP9hYmM="}


def test_empty_bytes():
    assert serialize_value(b"") == {"$bytes_b64": ""}


def test_nested_json_passthrough_and_recursion():
    value = {
        "a": 1,
        "b": [1, 2, {"c": None}],
        "d": {"e": decimal.Decimal("1.10"), "f": dt.date(2026, 1, 2)},
        "g": (1, b"x"),
    }
    out = serialize_value(value)
    assert out == {
        "a": 1,
        "b": [1, 2, {"c": None}],
        "d": {"e": "1.10", "f": "2026-01-02"},
        "g": [1, {"$bytes_b64": "eA=="}],
    }
    assert isinstance(out["g"], list)
    assert strict(out) == out


def test_dict_keys_become_strings():
    out = serialize_value({1: "a", uuid.UUID(int=1): "b", None: "c"})
    assert set(out) == {"1", "00000000-0000-0000-0000-000000000001", "None"}
    strict(out)


def test_nested_non_finite_floats():
    out = serialize_value({"x": [float("nan"), float("inf")]})
    assert out == {"x": ["NaN", "Infinity"]}
    strict(out)


@pytest.mark.parametrize(
    "value,expected",
    [
        (float("nan"), "NaN"),
        (float("inf"), "Infinity"),
        (float("-inf"), "-Infinity"),
    ],
)
def test_non_finite_floats_are_strings(value, expected):
    out = serialize_value(value)
    assert out == expected
    strict(out)


def test_finite_float_unchanged():
    assert serialize_value(0.0) == 0.0
    assert serialize_value(-1e300) == -1e300


def test_huge_int_preserved_as_int():
    n = 2**200 + 1
    out = serialize_value(n)
    assert out == n
    assert isinstance(out, int)
    assert strict(out) == n


def test_bool_not_converted_to_int():
    assert serialize_value(True) is True


def test_unknown_type_uses_str():
    class Weird:
        def __str__(self):
            return "weird!"

    assert serialize_value(Weird()) == "weird!"
    assert serialize_value({1, 2}.__class__) == str({1, 2}.__class__)
    assert serialize_value(complex(1, 2)) == "(1+2j)"


def test_str_subclass_and_int_subclass_pass_through():
    class Color(str, enum.Enum):
        RED = "red"

    class Num(enum.IntEnum):
        ONE = 1

    assert strict(serialize_value(Color.RED)) in ("red", "Color.RED")
    assert strict(serialize_value(Num.ONE)) == 1


def test_serialize_row_returns_list():
    row = (1, decimal.Decimal("2.5"), None, b"a")
    out = serialize_row(row)
    assert isinstance(out, list)
    assert out == [1, "2.5", None, {"$bytes_b64": "YQ=="}]


def test_serialize_row_accepts_row_like_sequence():
    class FakeRow:
        def __iter__(self):
            return iter((1, 2))

    assert serialize_row(FakeRow()) == [1, 2]


# build_result ---------------------------------------------------------------


def test_build_result_basic():
    res = build_result(["id", "total"], [(1, decimal.Decimal("12.50"))], 10, 1024)
    assert res == {
        "columns": ["id", "total"],
        "rows": [[1, "12.50"]],
        "row_count": 1,
        "truncated": False,
    }
    strict(res)


def test_build_result_empty_columns():
    res = build_result([], [], 10, 1024)
    assert res == {"columns": [], "rows": [], "row_count": 0, "truncated": False}
    strict(res)


def test_build_result_columns_but_no_rows():
    res = build_result(["a"], [], 10, 1024)
    assert res["columns"] == ["a"]
    assert not res["rows"]
    assert res["truncated"] is False


def test_columns_are_plain_strings_list():
    res = build_result(("a", "b"), [], 10, 1024)
    assert res["columns"] == ["a", "b"]
    assert isinstance(res["columns"], list)


def test_exactly_limit_rows_not_truncated():
    res = build_result(["n"], [(i,) for i in range(3)], 3, 10_000)
    assert res["row_count"] == 3
    assert res["truncated"] is False


def test_more_than_limit_rows_truncated():
    # The runner fetches limit+1; the extra row must be dropped.
    res = build_result(["n"], [(i,) for i in range(4)], 3, 10_000)
    assert res["rows"] == [[0], [1], [2]]
    assert res["row_count"] == 3
    assert res["truncated"] is True


def test_limit_zero():
    res = build_result(["n"], [(1,)], 0, 10_000)
    assert not res["rows"]
    assert res["row_count"] == 0
    assert res["truncated"] is True
    res = build_result(["n"], [], 0, 10_000)
    assert res["truncated"] is False


def test_accepts_iterator_of_rows():
    res = build_result(["n"], iter([(1,), (2,), (3,)]), 2, 10_000)
    assert res["rows"] == [[1], [2]]
    assert res["truncated"] is True


def test_byte_budget_truncation_stops_before_overflowing_row():
    rows = [("x" * 50,) for _ in range(10)]
    one = len(json.dumps(["x" * 50], separators=(",", ":")))
    base = len(json.dumps(["c"], separators=(",", ":")))
    budget = base + 3 * one + 4  # exactly three rows: '[' + 3 rows + 2 commas + ']'
    res = build_result(["c"], rows, 100, budget)
    assert res["row_count"] == 3
    assert res["truncated"] is True
    assert len(res["rows"]) == 3
    assert len(json.dumps(res["rows"], separators=(",", ":"))) + base <= budget


def test_byte_budget_exact_fit_not_truncated():
    rows = [("abc",), ("def",)]
    full = build_result(["c"], rows, 100, 10**6)
    size = len(json.dumps(full["columns"], separators=(",", ":"))) + len(
        json.dumps(full["rows"], separators=(",", ":"))
    )
    again = build_result(["c"], rows, 100, size)
    assert again["row_count"] == 2
    assert again["truncated"] is False
    smaller = build_result(["c"], rows, 100, size - 1)
    assert smaller["row_count"] == 1
    assert smaller["truncated"] is True


def test_first_row_exceeds_budget_returns_zero_rows_truncated():
    res = build_result(["c"], [("y" * 1000,)], 10, 100)
    assert not res["rows"]
    assert res["row_count"] == 0
    assert res["truncated"] is True
    assert res["columns"] == ["c"]
    strict(res)


def test_budget_measures_utf8_bytes_not_characters():
    row = ("é" * 40,)  # 80 bytes of UTF-8 payload when encoded raw
    ascii_size = len(json.dumps(list(row), separators=(",", ":"), ensure_ascii=False).encode())
    res = build_result([], [row], 10, ascii_size - 1)
    assert not res["rows"]
    assert res["truncated"] is True
    res = build_result([], [row], 10, ascii_size + 4)
    assert res["row_count"] == 1


def test_huge_budget_and_limit_full_result_round_trips():
    rows = [
        (
            i,
            decimal.Decimal(i) / 3,
            dt.datetime(2026, 1, 1),
            uuid.UUID(int=i),
            b"\x00\x01",
            {"k": [float("nan")]},
            2**100,
        )
        for i in range(20)
    ]
    cols = ["a", "b", "c", "d", "e", "f", "g"]
    res = build_result(cols, rows, 1000, 8 * 1024 * 1024)
    assert res["row_count"] == 20
    assert res["truncated"] is False
    assert strict(res) == res


def test_unserialisable_dict_cycle_not_supported_message():
    # Self-referencing containers cannot come from a database driver; ensure
    # we fail loudly (RecursionError) rather than hang.
    d = {}
    d["self"] = d
    with pytest.raises(RecursionError):
        serialize_value(d)


def test_module_has_no_salt_dunders():
    with open(serialize.__file__, encoding="utf-8") as fh:
        src = fh.read()
    assert "__salt__" not in src
    assert "__opts__" not in src
    assert "sqlalchemy" not in src


# --- T11 D3: a value longer than max_bytes is dropped without encoding it ------------------------
def test_value_longer_than_the_byte_cap_drops_the_row_without_encoding_it(monkeypatch):
    calls = []
    real = serialize.serialize_row
    monkeypatch.setattr(serialize, "serialize_row", lambda row: calls.append(row) or real(row))
    out = serialize.build_result(["c"], [("x" * 5000,), (1,)], 10, 1000)
    assert out == {"columns": ["c"], "rows": [], "row_count": 0, "truncated": True}
    assert not calls


def test_value_just_inside_the_byte_cap_is_still_measured_exactly():
    out = serialize.build_result(["c"], [("x" * 80,)], 10, 100)
    assert out["row_count"] == 1 and out["truncated"] is False
    out = serialize.build_result(["c"], [(b"x" * 500,)], 10, 100)
    assert out["row_count"] == 0 and out["truncated"] is True
