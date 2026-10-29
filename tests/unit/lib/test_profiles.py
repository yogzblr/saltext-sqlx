"""Unit tests for saltext.sqlx.lib.profiles (TEST_PLAN section 3)."""

import copy
import inspect
import json

import pytest
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import options
from saltext.sqlx.lib import profiles

BASE = {
    "dialect": "postgresql",
    "driver": "psycopg",
    "host": "db1.internal",
    "port": 5432,
    "database": "orders",
    "username": "salt_ro",
    "password": "sdb://vault/orders_pg",
}


def make(**overrides):
    prof = copy.deepcopy(BASE)
    prof.update(overrides)
    return prof


def config(**profs):
    return {"profiles": profs or {"orders_pg": make()}}


def _fails(code, func, *args):
    with pytest.raises(SaltInvocationError) as exc:
        func(*args)
    assert str(exc.value).startswith(f"[{code}]"), str(exc.value)
    return str(exc.value)


def test_valid_profile_loads_with_defaults():
    prof = profiles.load_profile("orders_pg", config())
    assert prof["name"] == "orders_pg"
    assert prof["dialect"] == "postgresql" and prof["driver"] == "psycopg"
    assert prof["host"] == "db1.internal" and prof["port"] == 5432
    assert prof["query"] == {} and prof["connect_args"] == {}
    assert prof["read_only"] is True
    assert prof["allow_execute"] is False
    assert prof["execute_verbs"] == ["INSERT", "UPDATE", "DELETE"]
    assert prof["statement_timeout_ms"] == 30_000
    assert prof["connect_timeout_s"] == 10
    assert prof["row_limit"] == 500
    assert prof["max_bytes"] == 1024 * 1024
    assert prof["error_detail"] is False
    json.dumps(prof)


def test_sdb_password_is_allowed_and_not_resolved():
    prof = profiles.load_profile("orders_pg", config())
    assert prof["password"] == "sdb://vault/orders_pg"


def test_literal_password_is_kept():
    assert profiles.validate_profile("p", make(password="lit eral"))["password"] == "lit eral"


def test_password_is_optional():
    raw = make()
    del raw["password"]
    assert profiles.validate_profile("p", raw).get("password") is None


def test_null_password_is_e_secret_not_no_password():
    """An unresolvable sdb:// reference arrives as None from config.get; it must not mean 'no password'."""
    _fails("E_SECRET", profiles.validate_profile, "p", make(password=None))


@pytest.mark.parametrize("bad", ["", "sdb://", 5, ["x"]])
def test_bad_password(bad):
    _fails("E_PROFILE_INVALID", profiles.validate_profile, "p", make(password=bad))


def test_validation_does_not_mutate_input():
    raw = make()
    before = copy.deepcopy(raw)
    profiles.validate_profile("p", raw)
    assert raw == before


@pytest.mark.parametrize("key", ["dialect", "host", "port", "database", "username"])
def test_required_keys(key):
    raw = make()
    del raw[key]
    msg = _fails("E_PROFILE_INVALID", profiles.validate_profile, "p", raw)
    assert key in msg


def test_unknown_key_is_rejected():
    msg = _fails("E_PROFILE_INVALID", profiles.validate_profile, "p", make(hots="x"))
    assert "hots" in msg


def test_unknown_key_message_never_contains_values():
    msg = _fails("E_PROFILE_INVALID", profiles.validate_profile, "p", make(oops="topsecret"))
    assert "topsecret" not in msg


@pytest.mark.parametrize("raw", [None, [], "x", 5])
def test_profile_must_be_a_mapping(raw):
    _fails("E_PROFILE_INVALID", profiles.validate_profile, "p", raw)


@pytest.mark.parametrize(
    "dialect,driver",
    [
        ("postgresql", "psycopg"),
        ("mysql", "pymysql"),
        ("oracle", "oracledb"),
        ("mssql", "pymssql"),
    ],
)
def test_dialect_driver_pairs_accepted(dialect, driver):
    prof = profiles.validate_profile("p", make(dialect=dialect, driver=driver))
    assert (prof["dialect"], prof["driver"]) == (dialect, driver)


@pytest.mark.parametrize(
    "dialect,driver",
    [
        ("postgresql", "pymysql"),
        ("mysql", "psycopg"),
        ("oracle", "pymssql"),
        ("mssql", "oracledb"),
        ("postgresql", "psycopg2"),
        ("mysql", "mysqlclient"),
        ("sqlite", "pysqlite"),
        ("postgresql", ""),
    ],
)
def test_dialect_driver_mismatch_is_rejected(dialect, driver):
    _fails(
        "E_PROFILE_INVALID", profiles.validate_profile, "p", make(dialect=dialect, driver=driver)
    )


def test_unknown_dialect_without_driver_is_rejected():
    raw = make(dialect="sqlite")
    del raw["driver"]
    _fails("E_PROFILE_INVALID", profiles.validate_profile, "p", raw)


def test_driver_defaults_from_dialect():
    raw = make(dialect="mysql")
    del raw["driver"]
    assert profiles.validate_profile("p", raw)["driver"] == "pymysql"


@pytest.mark.parametrize(
    "key,value",
    [
        ("host", ""),
        ("host", 5),
        ("port", "5432"),
        ("port", 0),
        ("port", 65536),
        ("port", True),
        ("database", ""),
        ("database", None),
        ("username", ""),
        ("username", 1),
        ("query", []),
        ("query", {"a": {"b": 1}}),
        ("query", {1: "a"}),
        ("connect_args", "x"),
        ("connect_args", []),
        ("read_only", "true"),
        ("allow_execute", 1),
        ("error_detail", "no"),
        ("execute_verbs", "INSERT"),
        ("execute_verbs", [1]),
        ("execute_verbs", ["IN SERT"]),
        ("execute_verbs", [""]),
        ("statement_timeout_ms", "1"),
        ("statement_timeout_ms", 0),
        ("statement_timeout_ms", True),
        ("connect_timeout_s", 0),
        ("row_limit", 0),
        ("row_limit", 1.5),
        ("max_bytes", -1),
    ],
)
def test_wrong_types_are_rejected(key, value):
    msg = _fails("E_PROFILE_INVALID", profiles.validate_profile, "p", make(**{key: value}))
    assert key in msg


@pytest.mark.parametrize(
    "key,value",
    [
        ("statement_timeout_ms", 300_001),
        ("connect_timeout_s", 61),
        ("row_limit", 10_001),
        ("max_bytes", 8 * 1024 * 1024 + 1),
    ],
)
def test_hard_maxima_are_enforced(key, value):
    _fails("E_PROFILE_INVALID", profiles.validate_profile, "p", make(**{key: value}))


@pytest.mark.parametrize(
    "key,value",
    [
        ("statement_timeout_ms", 300_000),
        ("connect_timeout_s", 60),
        ("row_limit", 10_000),
        ("max_bytes", 8 * 1024 * 1024),
    ],
)
def test_hard_maxima_are_inclusive(key, value):
    assert profiles.validate_profile("p", make(**{key: value}))[key] == value


def test_optional_values_are_kept():
    prof = profiles.validate_profile(
        "p",
        make(
            query={"sslmode": "verify-full", "n": 1, "f": 1.5, "b": True},
            connect_args={"application_name": "x"},
            read_only=False,
            allow_execute=True,
            execute_verbs=["insert", "Update"],
            error_detail=True,
            row_limit=5,
        ),
    )
    assert prof["query"] == {"sslmode": "verify-full", "n": 1, "f": 1.5, "b": True}
    assert prof["connect_args"] == {"application_name": "x"}
    assert prof["read_only"] is False and prof["allow_execute"] is True
    assert prof["execute_verbs"] == ["INSERT", "UPDATE"]
    assert prof["error_detail"] is True and prof["row_limit"] == 5


def test_returned_nested_values_are_copies():
    raw = make(query={"a": "b"})
    prof = profiles.validate_profile("p", raw)
    prof["query"]["a"] = "changed"
    assert raw["query"]["a"] == "b"


# ---- load_profile -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg",
    [None, {}, {"profiles": None}, {"profiles": {}}, {"profiles": {"other": make()}}, "junk"],
)
def test_unknown_profile(cfg):
    _fails("E_PROFILE_UNKNOWN", profiles.load_profile, "orders_pg", cfg)


@pytest.mark.parametrize("name", [None, 5, "", ["orders_pg"]])
def test_bad_profile_name_is_unknown(name):
    _fails("E_PROFILE_UNKNOWN", profiles.load_profile, name, config())


def test_unknown_profile_does_not_list_other_profiles():
    msg = _fails("E_PROFILE_UNKNOWN", profiles.load_profile, "nope", config())
    assert "orders_pg" not in msg


def test_profiles_section_must_be_a_mapping():
    _fails("E_PROFILE_INVALID", profiles.load_profile, "x", {"profiles": ["x"]})


def test_only_the_requested_profile_is_validated():
    cfg = config(good=make(), broken={"dialect": "nope"})
    assert profiles.load_profile("good", cfg)["name"] == "good"
    _fails("E_PROFILE_INVALID", profiles.load_profile, "broken", cfg)


def test_load_returns_a_fresh_copy():
    cfg = config()
    first = profiles.load_profile("orders_pg", cfg)
    first["host"] = "elsewhere"
    assert profiles.load_profile("orders_pg", cfg)["host"] == "db1.internal"
    assert cfg["profiles"]["orders_pg"]["host"] == "db1.internal"


# ---- the call can never override connection details ----------------------------------------


@pytest.mark.parametrize("key", ["host", "port", "driver", "url", "connect_args", "dialect"])
def test_call_cannot_override_connection_details(key):
    """Call kwargs never reach the profile: options rejects them before load_profile runs."""
    kwargs = {"profile": "orders_pg", "sql": "select 1", key: "evil"}
    with pytest.raises(SaltInvocationError) as exc:
        options.validate("query", kwargs)
    assert str(exc.value).startswith("[E_OPTION_UNKNOWN]")


def test_profile_caps_feed_option_caps():
    prof = profiles.load_profile("orders_pg", config())
    opts = options.validate("query", {"profile": "orders_pg", "sql": "select 1", "limit": 10**6})
    out = options.apply_caps(opts, prof)
    assert out["limit"] == prof["row_limit"] == 500
    assert out["timeout_ms"] == 30_000
    assert out["max_bytes"] == 1024 * 1024


def test_load_profile_signature_takes_only_name_and_config():
    assert list(inspect.signature(profiles.load_profile).parameters) == ["name", "config"]


@pytest.mark.parametrize("key", ["query", "connect_args"])
def test_null_mapping_means_empty(key):
    assert profiles.validate_profile("p", make(**{key: None}))[key] == {}
