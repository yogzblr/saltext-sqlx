"""PostgreSQL driver behaviour against a REAL server (SPEC decision D15, docs/WIRE_SHAPES.md section 16).

Runs with the session's PostgreSQL driver: pg8000 by default, psycopg with ``SQLX_PG_DRIVER=psycopg``. Tests that
assert something only one driver does say so and skip for the other. Server and accounts are those of
``tests-support/env.sh`` (PostgreSQL 54329; ``ro``, ``rw``; admin ``postgres``). The scratch table is dropped afterwards.
"""

# pylint: disable=redefined-outer-name,unused-argument
import base64
import datetime as dt
import decimal
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from salt.exceptions import CommandExecutionError

import saltext.sqlx.modules.sqlx_mod as mod
from saltext.sqlx.lib import dialects
from saltext.sqlx.lib import engine
from saltext.sqlx.lib import profiles
from tests.functional.test_runner_functional import HOST
from tests.functional.test_runner_functional import _reachable

pytestmark = pytest.mark.pg

# decided at import (skipif markers are evaluated at collection, before the session fixture swaps the default driver)
PG8000 = os.environ.get("SQLX_PG_DRIVER", "pg8000") == "pg8000"
PASSWORD_WITH_EVERYTHING = "p@ss:w/o%rd?x#y&=+ \"'\\é日"


@pytest.fixture(scope="module")
def server():
    srv, reason = _reachable("postgresql")
    if reason:
        pytest.skip(reason)
    srv.table = f"scratch.t16_{uuid.uuid4().hex[:10]}"
    srv.admin_run(f"CREATE TABLE {srv.table} (k varchar(20) PRIMARY KEY, n integer NOT NULL)")
    srv.admin_run(f"GRANT SELECT ON {srv.table} TO ro")
    srv.admin_run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {srv.table} TO rw")
    try:
        yield srv
    finally:
        srv.admin_run(f"DROP TABLE IF EXISTS {srv.table}")


@pytest.fixture
def salt(server, monkeypatch):
    base = {
        "dialect": "postgresql",
        "host": HOST,
        "port": server.cfg["port"],
        "database": server.cfg["database"],
        "connect_timeout_s": 5,
    }
    cfg = {
        "ro": dict(base, username="ro", password="ro_pw"),
        "rw": dict(base, username="rw", password="rw_pw", allow_execute=True, read_only=False),
        "detail": dict(base, username="rw", password="rw_pw", read_only=False, error_detail=True),
        "bad": dict(base, username="ro", password="not-the-password"),
        "nodb": dict(base, username="ro", password="ro_pw", database="no_such_database"),
        "nopw": dict(base, username="ro"),
        "refused": dict(base, username="ro", password="x", port=1),
        "tiny": dict(base, username="ro", password="ro_pw", statement_timeout_ms=700),
    }
    functions = {
        "config.get": lambda key, default=None: ({"profiles": cfg} if key == "sqlx" else default),
        "sdb.get": {}.__getitem__,
    }
    monkeypatch.setattr(mod, "__salt__", functions, raising=False)
    server.admin_run(f"DELETE FROM {server.table}")
    server.admin_run(f"INSERT INTO {server.table} (k, n) VALUES ('a', 1), ('b', 2)")
    return server


def fails(call, *args, **kw):
    with pytest.raises(CommandExecutionError) as exc:
        call(*args, **kw)
    return str(exc.value)


# -- the real error text, as the owner sees it ----------------------------------------------------------------------------------
def test_missing_table_keeps_the_real_text(salt):
    assert (
        fails(mod.query, "ro", "SELECT * FROM scratch.t16_missing")
        == '[E_SQL] postgresql 42P01: relation "scratch.t16_missing" does not exist'
    )


def test_syntax_error_keeps_the_real_text(salt):
    assert (
        fails(mod.query, "ro", "selec 1")
        == '[E_SQL] postgresql 42601: syntax error at or near "selec"'
    )


def test_bad_password(salt):
    text = fails(mod.ping, "bad")
    assert text.startswith("[E_AUTH_FAILED] postgresql")
    assert 'password authentication failed for user "ro"' in text
    assert "not-the-password" not in text
    if PG8000:
        assert text.startswith("[E_AUTH_FAILED] postgresql 28P01: ")  # pg8000 reports the SQLSTATE


def test_unknown_database(salt):
    text = fails(mod.ping, "nodb")
    assert text.startswith("[E_CONNECT] postgresql")
    assert 'database "no_such_database" does not exist' in text
    if PG8000:
        assert text.startswith("[E_CONNECT] postgresql 3D000: ")


def test_connection_refused(salt):
    text = fails(mod.ping, "refused")
    assert text.startswith("[E_CONNECT] postgresql") and "Connection refused" in text


def test_no_password_for_a_server_that_wants_one(salt):
    text = fails(mod.ping, "nopw")
    assert text.startswith("[E_AUTH_FAILED] postgresql")
    assert ("no password was provided" if PG8000 else "no password supplied") in text


def test_data_and_constraint_errors(salt):
    assert fails(mod.query, "ro", "SELECT 1/0") == "[E_SQL_DATA] postgresql 22012: division by zero"
    text = fails(mod.execute, "rw", f"INSERT INTO {salt.table} (k, n) VALUES ('a', 5)")
    assert text.startswith("[E_CONSTRAINT] postgresql 23505: duplicate key value violates")
    text = fails(mod.execute, "rw", f"INSERT INTO {salt.table} (k, n) VALUES ('z', NULL)")
    assert text.startswith("[E_CONSTRAINT] postgresql 23502: null value in column")


def test_invalid_input_value_is_hidden_unless_error_detail(salt):
    plain = fails(mod.query, "ro", "SELECT CAST(:v AS integer)", params={"v": "s3cr3t-text"})
    assert plain == '[E_SQL_DATA] postgresql 22P02: invalid input syntax for type integer: "..."'
    detailed = fails(mod.query, "detail", "SELECT CAST(:v AS integer)", params={"v": "s3cr3t-text"})
    assert "s3cr3t-text" in detailed


def test_write_in_a_read_only_session(salt):
    text = fails(mod.query, "ro", f"INSERT INTO {salt.table} (k, n) VALUES ('q', 1)")
    assert text.startswith("[E_READ_ONLY] postgresql 25006: ") or text.startswith(
        "[E_SQL] postgresql 42501"
    )


def test_statement_timeout(salt):
    text = fails(mod.query, "tiny", "SELECT pg_sleep(5)")
    assert text == "[E_TIMEOUT] postgresql 57014: canceling statement due to statement timeout"


@pytest.mark.skipif(not PG8000, reason="pg8000 socket handling")
def test_socket_read_timeout_is_the_statement_timeout_plus_a_second_not_the_connect_timeout(salt):
    raw = {
        "dialect": "postgresql",
        "host": HOST,
        "port": salt.cfg["port"],
        "database": salt.cfg["database"],
        "username": "ro",
        "connect_timeout_s": 2,
        "statement_timeout_ms": 4000,
    }
    prof = profiles.validate_profile("t", raw)
    with engine.connection(prof, "ro_pw") as conn:
        assert conn.connection.driver_connection._usock.gettimeout() == 5.0  # not 2.0


# -- one statement per call (T11 D1) ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1;SELECT 2",
        "COMMIT; CREATE TABLE scratch.t16_bypass (i int)",
        "SET statement_timeout = 0; SELECT pg_sleep(1)",
        "SET TRANSACTION READ WRITE; SELECT 1",
    ],
)
def test_a_second_statement_is_refused_by_the_server(salt, sql):
    text = fails(mod.query, "ro", sql)
    assert (
        text
        == "[E_SQL] postgresql 42601: cannot insert multiple commands into a prepared statement"
    )
    assert salt.admin_run("SELECT to_regclass('scratch.t16_bypass')") == [(None,)]


def test_a_second_statement_is_refused_for_execute_too(salt):
    text = fails(mod.execute, "rw", f"UPDATE {salt.table} SET n = 9; DELETE FROM {salt.table}")
    assert text.startswith("[E_SQL] postgresql 42601: cannot insert multiple commands")
    assert salt.verify(f"SELECT count(*) FROM {salt.table}") == [(2,)]


def test_a_second_statement_is_refused_in_a_streamed_query(salt):
    out = mod.query("ro", "SELECT i FROM generate_series(1, 5) AS i")
    assert out["row_count"] == 5
    text = fails(mod.query, "ro", "SELECT i FROM generate_series(1, 5) AS i; SELECT 2")
    assert text.startswith("[E_SQL] postgresql 42601")


def test_trailing_semicolon_and_semicolon_in_a_string_are_fine(salt):
    assert mod.query("ro", "SELECT 'a;b' AS s;")["rows"] == [["a;b"]]


# -- percent signs and colons -------------------------------------------------------------------------------------------------
def test_percent_sign_is_data_for_the_driver_not_a_placeholder(salt):
    out = mod.query("ro", "SELECT 100 % 7 AS m, 'a%b' LIKE '%b' AS l, '%s %d %(x)s' AS p")
    assert out["rows"] == [[2, True, "%s %d %(x)s"]]
    out = mod.query("ro", "SELECT 100 % 7 AS m, CAST(:p AS text) AS p", params={"p": "50%"})
    assert out["rows"] == [[2, "50%"]]


def test_literal_colon_and_cast_rules_are_unchanged(salt):
    out = mod.query(
        "ro",
        "SELECT 'a\\:b' AS c, (ARRAY[1, 2, 3])[1\\:2] AS s, CAST(:x AS integer) AS i",
        params={"x": 7},
    )
    assert out["rows"] == [["a:b", [1, 2], 7]]
    assert fails(mod.query, "ro", "SELECT :x::integer", params={"x": 1}).startswith(
        "[E_SQL] postgresql 42601"
    )


# -- bound values ---------------------------------------------------------------------------------------------------------------
BIND_CASES = [
    (1, 1),
    (2**40, 2**40),
    (True, True),
    (1.5, 1.5),
    ("héllo 日", "héllo 日"),
    (decimal.Decimal("12.50"), "12.50"),
    (b"\x00\x01\xff", {"$bytes_b64": base64.b64encode(b"\x00\x01\xff").decode()}),
    (dt.date(2020, 1, 2), "2020-01-02"),
    (dt.datetime(2020, 1, 2, 3, 4, 5), "2020-01-02T03:04:05"),
    (dt.datetime(2020, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc), "2020-01-02T03:04:05+00:00"),
    (dt.time(3, 4, 5), "03:04:05"),
    (uuid.UUID(int=5), "00000000-0000-0000-0000-000000000005"),
    (None, None),
]


@pytest.mark.parametrize(
    "value,expected", BIND_CASES, ids=[type(v).__name__ for v, _ in BIND_CASES]
)
def test_a_bare_bind_comes_back_typed_like_psycopg_returns_it(salt, value, expected):
    assert mod.query("ro", "SELECT :v AS v", params={"v": value})["rows"] == [[expected]]


def test_two_binds_in_one_expression(salt):
    """Without declared types the server reads both as unknown: 'operator is not unique: unknown + unknown'."""
    assert mod.query("ro", "SELECT :a + :b AS s, :a = :b AS e", params={"a": 2, "b": 3})[
        "rows"
    ] == [[5, False]]


def test_small_integers_keep_working_where_a_function_wants_int4(salt):
    assert mod.query("ro", "SELECT length(repeat('x', :n)) AS n", params={"n": 12})["rows"] == [
        [12]
    ]


def test_none_with_and_without_a_cast(salt):
    assert mod.query("ro", "SELECT CAST(:v AS integer) AS v", params={"v": None})["rows"] == [
        [None]
    ]
    assert mod.query("ro", "SELECT :v AS v", params={"v": None})["rows"] == [[None]]
    rows = mod.query("ro", f"SELECT n FROM {salt.table} WHERE k = :k", params={"k": None})["rows"]
    assert not rows  # `= NULL` matches nothing


def test_arrays_and_jsonb_with_a_cast(salt):
    out = mod.query(
        "ro",
        "SELECT CAST(:a AS integer[]) AS a, CAST(:t AS text[]) AS t, CAST(:j AS jsonb) AS j",
        params={"a": [1, 2, 3], "t": ["x", "y,z", 'q"r'], "j": '{"k": [1, 2], "s": "v"}'},
    )
    assert out["rows"] == [[[1, 2, 3], ["x", "y,z", 'q"r'], {"k": [1, 2], "s": "v"}]]
    assert mod.query(
        "ro", "SELECT :n = ANY(CAST(:ids AS integer[])) AS hit", params={"n": 2, "ids": [1, 2]}
    )["rows"] == [[True]]


@pytest.mark.skipif(not PG8000, reason="psycopg refuses a dict without a Json wrapper")
def test_a_dict_bind_is_json_text_with_pg8000(salt):
    out = mod.query("ro", "SELECT CAST(:d AS jsonb) AS d", params={"d": {"k": [1, {"z": None}]}})
    assert out["rows"] == [[{"k": [1, {"z": None}]}]]


def test_nul_in_a_bound_string_is_a_data_error(salt):
    text = fails(mod.query, "ro", "SELECT CAST(:v AS text)", params={"v": "a\x00b"})
    assert text.startswith("[E_SQL_DATA] postgresql")


def test_binds_in_a_where_clause_and_an_insert(salt):
    assert mod.query("ro", f"SELECT n FROM {salt.table} WHERE k = :k", params={"k": "b"})[
        "rows"
    ] == [[2]]
    out = mod.execute(
        "rw", f"INSERT INTO {salt.table} (k, n) VALUES (:k, :n)", params={"k": "c", "n": 3}
    )
    assert out["rows_affected"] == 1
    assert salt.verify(f"SELECT k, n FROM {salt.table} ORDER BY k") == [
        ("a", 1),
        ("b", 2),
        ("c", 3),
    ]


# -- values the server can send ----------------------------------------------------------------------------------------------
@pytest.mark.skipif(not PG8000, reason="pg8000 behaviour")
@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT 'infinity'::timestamp AS v", "infinity"),
        ("SELECT '-infinity'::date AS v", "-infinity"),
        ("SELECT '12000-01-01'::timestamp AS v", "12000-01-01 00:00:00"),
        ("SELECT '1 mon'::interval AS v", "1 months"),
        ("SELECT '(1,2)'::point AS v", [1.0, 2.0]),
    ],
)
def test_pg8000_returns_what_psycopg_cannot_hold(salt, sql, expected):
    assert mod.query("ro", sql)["rows"] == [[expected]]


@pytest.mark.skipif(PG8000, reason="psycopg behaviour")
def test_psycopg_cannot_hold_infinity(salt):
    assert fails(mod.query, "ro", "SELECT 'infinity'::timestamp AS v").startswith("[E_SQL_DATA]")


def test_common_types_serialize_to_strict_json(salt):
    out = mod.query(
        "ro",
        "SELECT '10.0.0.1'::inet AS i, '12.3'::money AS m, '{1,2}'::int[] AS a, 1.5::real AS r, "
        "'00:11:22:33:44:55'::macaddr AS mac, 'NaN'::float8 AS nan, now() AS ts",
    )
    json.dumps(out, allow_nan=False)
    assert out["rows"][0][:6] == ["10.0.0.1", "$12.30", [1, 2], 1.5, "00:11:22:33:44:55", "NaN"]


# -- large results ------------------------------------------------------------------------------------------------------------------
def test_a_large_result_is_cut_at_the_row_limit_without_reading_it_all(salt):
    out = mod.query(
        "ro",
        "SELECT i, repeat('x', 200) AS pad FROM generate_series(1, 2000000) AS i",
        limit=100,
    )
    assert out["row_count"] == 100 and out["truncated"] is True


def test_the_connection_is_usable_after_an_error_mid_result(salt):
    assert fails(mod.query, "ro", "SELECT 10 / (i - 3) FROM generate_series(1, 5) AS i").startswith(
        "[E_SQL_DATA] postgresql 22012"
    )
    assert mod.query("ro", "SELECT 1 AS ok")["rows"] == [[1]]


# -- connection details ------------------------------------------------------------------------------------------------------------
def test_password_with_every_awkward_character(salt):
    admin = salt
    admin.admin_run("DROP ROLE IF EXISTS t16_weird")
    stmt = admin.admin_run(
        "SELECT format('CREATE ROLE t16_weird LOGIN PASSWORD %L', CAST(:pw AS text))",
        {"pw": PASSWORD_WITH_EVERYTHING},
    )[0][0]
    eng = sa.create_engine(
        admin.url("admin"), poolclass=sa.pool.NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql(stmt.replace("%", "%%"))
            conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {salt.cfg['database']} TO t16_weird")
        raw = {
            "dialect": "postgresql",
            "host": HOST,
            "port": salt.cfg["port"],
            "database": salt.cfg["database"],
            "username": "t16_weird",
            "connect_timeout_s": 5,
        }
        prof = profiles.validate_profile("t", raw)
        assert engine.ping(prof, PASSWORD_WITH_EVERYTHING)["ok"] is True
        with pytest.raises(CommandExecutionError) as exc:
            engine.ping(prof, PASSWORD_WITH_EVERYTHING + "x")
        assert str(exc.value).startswith("[E_AUTH_FAILED]")
        assert PASSWORD_WITH_EVERYTHING not in str(exc.value)
    finally:
        with eng.connect() as conn:
            conn.exec_driver_sql(
                f"REVOKE CONNECT ON DATABASE {salt.cfg['database']} FROM t16_weird"
            )
            conn.exec_driver_sql("DROP ROLE IF EXISTS t16_weird")
        eng.dispose()


def test_application_name_from_a_profile(salt, monkeypatch):
    cfg = mod.__salt__["config.get"]("sqlx")["profiles"]
    cfg["named"] = dict(cfg["ro"], connect_args={"application_name": "salt sqlx host=evil port=1"})
    assert mod.query("named", "SHOW application_name")["rows"] == [["salt sqlx host=evil port=1"]]


def test_the_driver_in_use_is_the_one_asked_for(salt):
    prof = profiles.validate_profile(
        "t",
        {
            "dialect": "postgresql",
            "host": HOST,
            "port": salt.cfg["port"],
            "database": salt.cfg["database"],
            "username": "ro",
        },
    )
    with engine.connection(prof, "ro_pw") as conn:
        assert conn.dialect.driver == dialects.DIALECT_DRIVERS["postgresql"]


@pytest.mark.parametrize("driver", ["pg8000", "psycopg"])
def test_each_driver_can_be_chosen_by_the_profile(salt, driver):
    prof = profiles.validate_profile(
        "t",
        {
            "dialect": "postgresql",
            "driver": driver,
            "host": HOST,
            "port": salt.cfg["port"],
            "database": salt.cfg["database"],
            "username": "ro",
        },
    )
    out = engine.ping(prof, "ro_pw")
    assert out["ok"] is True and out["server_version"]
    with engine.connection(prof, "ro_pw") as conn:
        assert conn.dialect.driver == driver
