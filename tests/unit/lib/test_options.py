"""Unit tests for saltext.sqlx.lib.options (TEST_PLAN section 3)."""

import json

import pytest
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import options

FUNCTIONS = (
    "query",
    "execute",
    "check_query_result",
    "check_row_count",
    "ping",
    "table_list",
)

ALLOWED = {
    "query": {"profile", "sql", "params", "limit", "timeout_ms", "max_bytes"},
    "execute": {"profile", "sql", "params", "dry_run", "timeout_ms"},
    "check_query_result": {
        "profile",
        "sql",
        "operator",
        "expected",
        "params",
        "row",
        "column",
        "retry_timeout_s",
        "retry_pause_s",
    },
    "check_row_count": {
        "profile",
        "sql",
        "operator",
        "expected",
        "params",
        "retry_timeout_s",
        "retry_pause_s",
    },
    "ping": {"profile"},
    "table_list": {"profile", "schema", "pattern", "include_views"},
}

MINIMAL = {
    "query": {"profile": "p", "sql": "select 1"},
    "execute": {"profile": "p", "sql": "update t set a = 1"},
    "check_query_result": {
        "profile": "p",
        "sql": "select 1",
        "operator": "==",
        "expected": 1,
    },
    "check_row_count": {"profile": "p", "sql": "select 1", "operator": ">=", "expected": 1},
    "ping": {"profile": "p"},
    "table_list": {"profile": "p"},
}


def _fails(code, func, kwargs):
    with pytest.raises(SaltInvocationError) as exc:
        options.validate(func, kwargs)
    assert str(exc.value).startswith(f"[{code}]"), str(exc.value)
    return str(exc.value)


@pytest.mark.parametrize("func", FUNCTIONS)
def test_minimal_call_is_accepted(func):
    out = options.validate(func, dict(MINIMAL[func]))
    assert out["profile"] == "p"
    json.dumps(out)


@pytest.mark.parametrize("func", FUNCTIONS)
def test_dunder_keys_are_dropped(func):
    kwargs = dict(MINIMAL[func])
    kwargs.update({"__pub_user": "bob", "__pub_jid": "1", "__kwarg__": True, "__anything": 1})
    out = options.validate(func, kwargs)
    assert not [k for k in out if k.startswith("__")]
    assert set(out) <= ALLOWED[func]


def test_input_is_not_mutated():
    kwargs = {"profile": "p", "sql": "select 1", "__pub_user": "bob"}
    options.validate("query", kwargs)
    assert kwargs == {"profile": "p", "sql": "select 1", "__pub_user": "bob"}


@pytest.mark.parametrize("func", FUNCTIONS)
def test_unknown_key_is_rejected(func):
    kwargs = dict(MINIMAL[func], lmit=5)
    msg = _fails("E_OPTION_UNKNOWN", func, kwargs)
    assert "lmit" in msg


@pytest.mark.parametrize(
    "key", ["host", "port", "driver", "url", "connect_args", "password", "username", "database"]
)
@pytest.mark.parametrize("func", FUNCTIONS)
def test_connection_details_cannot_be_supplied(func, key):
    kwargs = dict(MINIMAL[func])
    kwargs[key] = "evil.example" if key != "connect_args" else {"ssl_context": 1}
    msg = _fails("E_OPTION_UNKNOWN", func, kwargs)
    assert "evil.example" not in msg


def test_unknown_key_for_one_function_is_valid_for_another():
    kwargs = dict(MINIMAL["query"], dry_run=True)
    _fails("E_OPTION_UNKNOWN", "query", kwargs)
    out = options.validate("execute", dict(MINIMAL["execute"], dry_run=True))
    assert out["dry_run"] is True


def test_unknown_function_is_a_programming_error():
    with pytest.raises(ValueError):
        options.validate("nope", {})


@pytest.mark.parametrize("func", ["query", "execute", "check_query_result", "check_row_count"])
@pytest.mark.parametrize("key", ["profile", "sql"])
def test_missing_required_key(func, key):
    kwargs = dict(MINIMAL[func])
    del kwargs[key]
    _fails("E_OPTION_INVALID", func, kwargs)


@pytest.mark.parametrize("func", ["check_query_result", "check_row_count"])
@pytest.mark.parametrize("key", ["operator", "expected"])
def test_check_missing_operator_or_expected(func, key):
    kwargs = dict(MINIMAL[func])
    del kwargs[key]
    _fails("E_OPTION_INVALID", func, kwargs)


def test_expected_may_be_null_or_any_json():
    for value in (None, 0, "x", [1], {"a": 1}):
        kwargs = dict(MINIMAL["check_query_result"], expected=value)
        assert options.validate("check_query_result", kwargs)["expected"] == value


def test_ping_requires_profile():
    _fails("E_OPTION_INVALID", "ping", {})


@pytest.mark.parametrize(
    "func,key,value",
    [
        ("query", "profile", 5),
        ("query", "profile", ""),
        ("query", "sql", ""),
        ("query", "sql", ["select 1"]),
        ("query", "params", [1, 2]),
        ("query", "params", "a=1"),
        ("query", "params", {1: "a"}),
        ("query", "limit", "10"),
        ("query", "limit", 1.5),
        ("query", "limit", True),
        ("query", "limit", 0),
        ("query", "limit", -1),
        ("query", "timeout_ms", "100"),
        ("query", "timeout_ms", 0),
        ("query", "max_bytes", 0),
        ("query", "max_bytes", "1k"),
        ("execute", "dry_run", "yes"),
        ("execute", "dry_run", 1),
        ("execute", "timeout_ms", -5),
        ("check_query_result", "operator", 5),
        ("check_query_result", "row", -1),
        ("check_query_result", "row", "0"),
        ("check_query_result", "column", 1.0),
        ("check_query_result", "column", True),
        ("check_query_result", "retry_timeout_s", "3"),
        ("check_query_result", "retry_timeout_s", -1),
        ("check_query_result", "retry_pause_s", -0.5),
        ("check_query_result", "retry_pause_s", True),
        ("table_list", "schema", 7),
        ("table_list", "pattern", ["a"]),
        ("table_list", "include_views", "true"),
    ],
)
def test_wrong_type_or_range_is_invalid(func, key, value):
    kwargs = dict(MINIMAL[func])
    kwargs[key] = value
    msg = _fails("E_OPTION_INVALID", func, kwargs)
    assert key in msg


def test_error_messages_never_contain_values():
    secret = "s3cr3t-value"
    kwargs = dict(MINIMAL["query"], params=[secret], sql=["select " + secret])
    msg = _fails("E_OPTION_INVALID", "query", kwargs)
    assert secret not in msg


def test_none_means_unset_for_optional_keys():
    kwargs = dict(MINIMAL["query"], params=None, limit=None, timeout_ms=None, max_bytes=None)
    out = options.validate("query", kwargs)
    assert out["limit"] is None and out["params"] is None


def test_valid_values_pass_through():
    kwargs = {
        "profile": "orders",
        "sql": "select :a",
        "params": {"a": 1, "b": [1, 2], "c": None},
        "limit": 10,
        "timeout_ms": 500,
        "max_bytes": 2048,
    }
    out = options.validate("query", kwargs)
    assert out["params"] == {"a": 1, "b": [1, 2], "c": None}
    assert (out["limit"], out["timeout_ms"], out["max_bytes"]) == (10, 500, 2048)


def test_check_defaults_and_numeric_types():
    out = options.validate("check_query_result", dict(MINIMAL["check_query_result"]))
    assert out["row"] == 0 and out["column"] == 0
    assert out["retry_timeout_s"] == 0 and out["retry_pause_s"] == 1
    out = options.validate(
        "check_row_count", dict(MINIMAL["check_row_count"], retry_timeout_s=2.5, retry_pause_s=0)
    )
    assert out["retry_timeout_s"] == 2.5 and out["retry_pause_s"] == 0


def test_execute_dry_run_default_false():
    assert options.validate("execute", dict(MINIMAL["execute"]))["dry_run"] is False


def test_table_list_defaults():
    out = options.validate("table_list", {"profile": "p"})
    assert out["include_views"] is False and out["schema"] is None and out["pattern"] is None


# ---- caps: the call may only lower them -----------------------------------------------------

PROFILE_CAPS = {"row_limit": 100, "statement_timeout_ms": 5000, "max_bytes": 4096}


def test_caps_default_to_profile_values():
    opts = options.validate("query", dict(MINIMAL["query"]))
    out = options.apply_caps(opts, PROFILE_CAPS)
    assert (out["limit"], out["timeout_ms"], out["max_bytes"]) == (100, 5000, 4096)


def test_caps_can_be_lowered():
    opts = options.validate("query", dict(MINIMAL["query"], limit=7, timeout_ms=10, max_bytes=9))
    out = options.apply_caps(opts, PROFILE_CAPS)
    assert (out["limit"], out["timeout_ms"], out["max_bytes"]) == (7, 10, 9)


@pytest.mark.parametrize(
    "key,value,expected_key,expected",
    [
        ("limit", 101, "limit", 100),
        ("limit", 10**9, "limit", 100),
        ("timeout_ms", 5001, "timeout_ms", 5000),
        ("max_bytes", 4097, "max_bytes", 4096),
    ],
)
def test_caps_can_never_be_raised(key, value, expected_key, expected):
    opts = options.validate("query", dict(MINIMAL["query"], **{key: value}))
    assert options.apply_caps(opts, PROFILE_CAPS)[expected_key] == expected


def test_caps_never_exceed_the_hard_maxima_even_with_a_bad_profile():
    huge = {"row_limit": 10**9, "statement_timeout_ms": 10**9, "max_bytes": 10**12}
    opts = options.validate("query", dict(MINIMAL["query"], limit=10**9, timeout_ms=10**9))
    out = options.apply_caps(opts, huge)
    assert out["limit"] == 10_000
    assert out["timeout_ms"] == 300_000
    assert out["max_bytes"] == 8 * 1024 * 1024


def test_hard_maxima_constants():
    assert options.HARD_MAX_ROWS == 10_000
    assert options.HARD_MAX_TIMEOUT_MS == 300_000
    assert options.HARD_MAX_BYTES == 8 * 1024 * 1024
    assert options.HARD_MAX_RETRY_TIMEOUT_S == 10


def test_execute_caps_only_touch_timeout():
    opts = options.validate("execute", dict(MINIMAL["execute"], timeout_ms=10**6))
    out = options.apply_caps(opts, PROFILE_CAPS)
    assert out["timeout_ms"] == 5000
    assert "limit" not in out and "max_bytes" not in out


def test_retry_timeout_is_capped_at_ten_seconds():
    opts = options.validate("check_query_result", dict(MINIMAL["check_query_result"]))
    opts["retry_timeout_s"] = 99
    assert options.apply_caps(opts, PROFILE_CAPS)["retry_timeout_s"] == 10
    opts["retry_timeout_s"] = 3
    assert options.apply_caps(opts, PROFILE_CAPS)["retry_timeout_s"] == 3


def test_check_query_gets_row_limit_and_timeout_caps():
    opts = options.validate("check_row_count", dict(MINIMAL["check_row_count"]))
    out = options.apply_caps(opts, PROFILE_CAPS)
    assert out["limit"] == 100 and out["timeout_ms"] == 5000 and out["max_bytes"] == 4096


def test_apply_caps_does_not_mutate_and_ping_is_untouched():
    opts = options.validate("ping", {"profile": "p"})
    out = options.apply_caps(opts, PROFILE_CAPS)
    assert out == opts and out is not opts
    assert "limit" not in out
