# pylint: disable=use-dict-literal,unused-argument,unused-variable,cell-var-from-loop,expression-not-assigned,bidirectional-unicode,use-implicit-booleaness-not-comparison,unnecessary-lambda-assignment,consider-using-with
"""T11 part 2, attack class (6): crash the module with hostile argument values.

The contract is: every call either returns strict JSON or raises ``CommandExecutionError`` / ``SaltInvocationError``
whose text starts with ``[E_...]``; never another exception type, never a hang. ``RecursionError``, ``MemoryError``
and ``UnicodeEncodeError`` from deep inside a driver must arrive as ``[E_INTERNAL]``.
"""

import json
import time

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

from tests.security import harness
from tests.security.harness import observe
from tests.security.test_attacks_query import defect


def deep_list(n):
    root = cur = []
    for _ in range(n):
        nxt = []
        cur.append(nxt)
        cur = nxt
    return root


def cast_text(server):
    return "SELECT CAST(:v AS text)" if server.key == "postgresql" else "SELECT CAST(:v AS CHAR)"


CASES = {
    "sql=None": lambda s: dict(sql=None),
    "sql=123": lambda s: dict(sql=123),
    "sql=list": lambda s: dict(sql=["SELECT 1"]),
    "sql=bytes": lambda s: dict(sql=b"SELECT 1"),
    "sql=dict": lambda s: dict(sql={"a": 1}),
    "sql=True": lambda s: dict(sql=True),
    "sql=float-nan": lambda s: dict(sql=float("nan")),
    "sql=empty": lambda s: dict(sql=""),
    "sql=spaces": lambda s: dict(sql="   "),
    "sql=lone-surrogate": lambda s: dict(sql="SELECT '\ud800'"),
    "sql=null-byte-only": lambda s: dict(sql="\x00"),
    "sql=5MB-comment": lambda s: dict(sql="SELECT 1 /*" + "x" * 5_000_000 + "*/"),
    "sql=10000-nested-parens": lambda s: dict(sql="SELECT " + "(" * 10000 + "1" + ")" * 10000),
    "sql=100k-union-terms": lambda s: dict(
        sql="SELECT 1" + " UNION ALL SELECT 1" * 100_000, timeout_ms=3000
    ),
    "sql=percent-and-braces": lambda s: dict(sql="SELECT '%s %(a)s {} {0} %%' AS a"),
    "sql=colon-forms": lambda s: dict(sql="SELECT ':a' AS a, '::b' AS b, '\\:c' AS c"),
    "params=100k-unused-keys": lambda s: dict(
        sql="SELECT 1", params={f"k{i}": i for i in range(100_000)}
    ),
    "params=nested-dict": lambda s: dict(sql=cast_text(s), params={"v": {"a": {"b": [1, 2]}}}),
    "params=20000-deep-list": lambda s: dict(sql=cast_text(s), params={"v": deep_list(20000)}),
    "params=nan": lambda s: dict(sql=cast_text(s), params={"v": float("nan")}),
    "params=inf": lambda s: dict(sql=cast_text(s), params={"v": float("inf")}),
    "params=10**40": lambda s: dict(sql=cast_text(s), params={"v": 10**40}),
    "params=10**5000": lambda s: dict(sql=cast_text(s), params={"v": 10**5000}),
    "params=bytes": lambda s: dict(sql=cast_text(s), params={"v": b"\x00\xff"}),
    "params=null-byte-str": lambda s: dict(sql=cast_text(s), params={"v": "a\x00b"}),
    "params=50MB-str": lambda s: dict(sql=cast_text(s), params={"v": "x" * 50_000_000}),
    "params=object": lambda s: dict(sql=cast_text(s), params={"v": object()}),
    "params=int-key": lambda s: dict(sql="SELECT 1", params={1: 2}),
    "params=list": lambda s: dict(sql="SELECT 1", params=[1]),
    "params=str": lambda s: dict(sql="SELECT 1", params="v"),
    "params=missing-bind": lambda s: dict(sql="SELECT :nope"),
    "params=weird-name": lambda s: dict(sql="SELECT :a", params={"a)--": 1}),
    "limit=10**30": lambda s: dict(sql="SELECT 1", limit=10**30),
    "limit=10**5000": lambda s: dict(sql="SELECT 1", limit=10**5000),
    "limit=0": lambda s: dict(sql="SELECT 1", limit=0),
    "limit=-1": lambda s: dict(sql="SELECT 1", limit=-1),
    "limit=True": lambda s: dict(sql="SELECT 1", limit=True),
    "limit=1.5": lambda s: dict(sql="SELECT 1", limit=1.5),
    "limit=nan": lambda s: dict(sql="SELECT 1", limit=float("nan")),
    "limit=inf": lambda s: dict(sql="SELECT 1", limit=float("inf")),
    "limit=str": lambda s: dict(sql="SELECT 1", limit="5"),
    "limit=list": lambda s: dict(sql="SELECT 1", limit=[5]),
    "timeout_ms=10**30": lambda s: dict(sql="SELECT 1", timeout_ms=10**30),
    "timeout_ms=0": lambda s: dict(sql="SELECT 1", timeout_ms=0),
    "timeout_ms=-5": lambda s: dict(sql="SELECT 1", timeout_ms=-5),
    "timeout_ms=1": lambda s: dict(sql="SELECT 1", timeout_ms=1),
    "max_bytes=0": lambda s: dict(sql="SELECT 1", max_bytes=0),
    "max_bytes=1": lambda s: dict(sql="SELECT 1", max_bytes=1),
    "max_bytes=10**30": lambda s: dict(sql="SELECT 1", max_bytes=10**30),
    "kw-name-newline": lambda s: {"sql": "SELECT 1", "a\r\nb": 1},
    "kw-name-100k": lambda s: {"sql": "SELECT 1", "k" * 100_000: 1},
    "kw-dunder": lambda s: {
        "sql": "SELECT 1",
        "__pub_user": "x\r\ny",
        "__kwarg__": True,
        "__pub_jid": object(),
    },
    "kw-non-ascii": lambda s: {"sql": "SELECT 1", "‮evil": 1},
}


@pytest.mark.parametrize("label", sorted(CASES))
def test_query_never_crashes_or_hangs(server, salt, label):
    kwargs = CASES[label](server)
    start = time.time()
    outcome = harness.attempt(lambda: salt.query("ro", **kwargs))
    seconds = time.time() - start
    observe(
        "robustness", f"{server.key} query {label}", f"{seconds:.1f}s {harness.describe(outcome)}"
    )
    assert seconds < 30
    if outcome[0] == "OK":
        json.dumps(outcome[1], allow_nan=False)
    else:
        assert isinstance(outcome[1], (CommandExecutionError, SaltInvocationError)), type(
            outcome[1]
        )
        assert str(outcome[1]).startswith("[E_"), str(outcome[1])[:200]


SERVER_VALUES = {
    "postgresql": [
        ("jsonb-20000-deep", "SELECT (repeat('[', 20000) || repeat(']', 20000))::jsonb"),
        ("jsonb-2000-deep", "SELECT (repeat('[', 2000) || repeat(']', 2000))::jsonb"),
        (
            "float-nan-inf",
            "SELECT 'NaN'::float8 AS a, 'Infinity'::float8 AS b, '-Infinity'::float8 AS c",
        ),
        ("numeric-nan", "SELECT 'NaN'::numeric AS a"),
        ("numeric-huge", "SELECT 1e100000::numeric AS a"),
        ("chr0", "SELECT chr(0)"),
        ("invalid-utf8", "SELECT convert_from('\\xff'::bytea, 'UTF8')"),
        ("bytea-huge", "SELECT decode(repeat('ab', 5000000), 'hex')"),
        ("timestamp-infinity", "SELECT 'infinity'::timestamp AS a, '-infinity'::date AS b"),
        ("timestamp-year-5874897", "SELECT '5874897-12-31'::date AS a"),
        ("interval-huge", "SELECT interval '178000000 years' AS a"),
        ("array-nested", "SELECT ARRAY[[1,2],[3,4]] AS a"),
        ("composite", "SELECT ROW(1, 'a', NULL) AS a"),
        ("range", "SELECT int4range(1, 10) AS a"),
        ("oid-and-xml", "SELECT 1::oid AS a, '<a/>'::xml AS b"),
    ],
    "mysql": [
        ("json-deep-90", "SELECT CAST(CONCAT(REPEAT('[', 90), REPEAT(']', 90)) AS JSON) AS a"),
        ("double-overflow", "SELECT 1e308 * 10 AS a"),
        ("zero-date", "SELECT CAST('0000-00-00' AS CHAR) AS a"),
        ("bit", "SELECT b'101' AS a"),
        ("blob-bytes", "SELECT UNHEX('00FF') AS a"),
        ("decimal-huge", "SELECT CAST(REPEAT('9', 60) AS DECIMAL(65, 0)) AS a"),
        ("null-bytes-in-text", "SELECT CONCAT('a', CHAR(0), 'b') AS a"),
        ("time-838", "SELECT CAST('838:59:59' AS TIME) AS a"),
    ],
}


def test_odd_server_values_are_returned_as_strict_json(server, salt):
    key = "postgresql" if server.key == "postgresql" else "mysql"
    for label, sql in SERVER_VALUES[key]:
        outcome = harness.attempt(lambda s=sql: salt.query("ro", s))
        observe("robustness", f"{server.key} value {label}", harness.describe(outcome))
        if outcome[0] == "OK":
            json.dumps(outcome[1], allow_nan=False)
        else:
            assert isinstance(outcome[1], (CommandExecutionError, SaltInvocationError)) and str(
                outcome[1]
            ).startswith("[E_")


CHECKS = [
    ("operator=None", dict(operator=None, expected=1)),
    ("operator=int", dict(operator=5, expected=1)),
    ("operator=unknown", dict(operator="~=", expected=1)),
    ("operator=empty", dict(operator="", expected=1)),
    ("expected-5000-deep", dict(operator="==", expected=deep_list(5000))),
    ("expected-50MB", dict(operator="contains", expected="a" * 50_000_000)),
    ("expected-dict-cycle-free", dict(operator="==", expected={"a": [1, {"b": None}]})),
    ("row=-1", dict(operator="==", expected=1, row=-1)),
    ("row=10**30", dict(operator="==", expected=1, row=10**30)),
    ("column=1.5", dict(operator="==", expected=1, column=1.5)),
    ("retry_pause_s=0", dict(operator="==", expected=1, retry_pause_s=0)),
    ("retry_pause_s=nan", dict(operator="==", expected=1, retry_pause_s=float("nan"))),
    ("retry_timeout_s=nan", dict(operator="==", expected=1, retry_timeout_s=float("nan"))),
    ("retry_timeout_s=-1", dict(operator="==", expected=1, retry_timeout_s=-1)),
    (
        "retry_timeout_s=inf",
        dict(operator="==", expected=2, retry_timeout_s=float("inf"), retry_pause_s=20),
    ),
]


@pytest.mark.parametrize("label,kwargs", CHECKS, ids=[c[0] for c in CHECKS])
def test_check_query_result_never_crashes_or_hangs(server, salt, label, kwargs):
    start = time.time()
    outcome = harness.attempt(lambda: salt.check_query_result("ro", "SELECT 1", **kwargs))
    seconds = time.time() - start
    observe(
        "robustness",
        f"{server.key} check_query_result {label}",
        f"{seconds:.1f}s {harness.describe(outcome)[:100]}",
    )
    assert seconds < 16
    if outcome[0] == "ERR":
        assert isinstance(outcome[1], (CommandExecutionError, SaltInvocationError)) and str(
            outcome[1]
        ).startswith("[E_")


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(schema="x'; DROP TABLE y; --"),
        dict(schema="scratch` -- "),
        dict(pattern="[[[(("),
        dict(pattern="\\"),
        dict(pattern="%" * 100000),
        dict(schema="s" * 100000),
        dict(schema=5),
        dict(pattern=["a"]),
        dict(include_views="yes"),
    ],
)
def test_table_list_never_crashes(server, salt, kwargs):
    outcome = harness.attempt(lambda: salt.table_list("ro", **kwargs))
    observe(
        "robustness", f"{server.key} table_list {str(kwargs)[:50]}", harness.describe(outcome)[:140]
    )
    if outcome[0] == "ERR":
        assert isinstance(outcome[1], (CommandExecutionError, SaltInvocationError)) and str(
            outcome[1]
        ).startswith("[E_")


# ---- reported defects -------------------------------------------------------------------------------------------------------------
def test_check_result_is_strict_json_even_for_a_nan_expected(request, server, salt):
    """D6: ``expected`` is echoed back as sent; a NaN float is not valid JSON (SPEC section 9)."""
    out = salt.check_query_result("ro", "SELECT 1", "==", expected=float("nan"))
    json.dumps(out, allow_nan=False)


def test_unknown_option_names_are_not_echoed_with_control_characters(request, server, salt):
    """D8: ``[E_OPTION_UNKNOWN]`` repeats the caller's key names verbatim (newlines, bidi controls, 100 000 characters)."""
    with pytest.raises(SaltInvocationError) as exc:
        salt.query(
            "ro", "SELECT 1", **{"a\r\nINFO forged log line": 1, "‮evil": 1, "k" * 100_000: 1}
        )
    text = str(exc.value)
    assert "\n" not in text and "\r" not in text and "‮" not in text and len(text) < 2000


def test_values_python_cannot_hold_are_a_data_error_not_internal(server, salt):
    """D7: psycopg raises DataError client-side for 'infinity' and for years after 9999; it must read as E_SQL_DATA."""
    if server.key != "postgresql":
        pytest.skip("psycopg DataError for dates beyond year 9999 is PostgreSQL-specific")
    for sql in (
        "SELECT 'infinity'::timestamp",
        "SELECT '-infinity'::date",
        "SELECT '5874897-12-31'::date",
    ):
        with pytest.raises(CommandExecutionError) as exc:
            salt.query("ro", sql)
        text = str(exc.value)
        observe("robustness", f"postgresql {sql}", text)
        assert text.startswith("[E_SQL_DATA]") and "infinity" not in text and "5874897" not in text
