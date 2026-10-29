"""Functional tests for lib/runner against real servers (TEST_PLAN section 4: query, execute, error fidelity).

Parametrised over PostgreSQL, MySQL and MariaDB. A dialect whose server is not reachable (or whose accounts are
missing) is skipped with the reason; start the servers with ``tests-support/up.sh auto``. Defaults match
``tests-support/env.sh``; override with ``SQLX_TEST_HOST``, ``SQLX_PG_PORT``, ``SQLX_MYSQL_PORT``,
``SQLX_MARIADB_PORT`` and ``SQLX_ADMIN_PW``.

Scratch tables are named ``t6_<random>`` and dropped afterwards; the seeded ``orders`` table is only read.
The admin account creates and verifies; ``ro`` and ``rw`` are the accounts under test. Verification always uses a
fresh connection on a fresh engine, so an uncommitted change on the connection under test cannot be seen.
"""

import base64
import contextlib
import datetime as dt
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from saltext.sqlx.lib import engine
from saltext.sqlx.lib import errors
from saltext.sqlx.lib import profiles
from saltext.sqlx.lib import runner

HOST = os.environ.get("SQLX_TEST_HOST", "127.0.0.1")
ADMIN_PW = os.environ.get("SQLX_ADMIN_PW", "admin_pw")
VERBS = ("insert", "update", "delete")

SERVERS = {
    "postgresql": {
        "dialect": "postgresql",
        "drivername": "postgresql+psycopg",
        "port": int(os.environ.get("SQLX_PG_PORT", "54329")),
        "database": "sqlx_test",
        "admin": "postgres",
    },
    "mysql": {
        "dialect": "mysql",
        "drivername": "mysql+pymysql",
        "port": int(os.environ.get("SQLX_MYSQL_PORT", "33061")),
        "database": "scratch",
        "admin": "root",
    },
    "mariadb": {
        "dialect": "mysql",
        "drivername": "mysql+pymysql",
        "port": int(os.environ.get("SQLX_MARIADB_PORT", "33062")),
        "database": "scratch",
        "admin": "root",
    },
}

DIALECTS = [
    pytest.param("postgresql", marks=pytest.mark.pg),
    pytest.param("mysql", marks=pytest.mark.mysql),
    pytest.param("mariadb", marks=pytest.mark.mariadb),
]


class Db:
    """One reachable server plus the scratch table used by the tests."""

    def __init__(self, key):
        self.key = key
        self.cfg = SERVERS[key]
        self.dialect = self.cfg["dialect"]  # the profile dialect (MariaDB is "mysql", SPEC D10)
        self.table = f"scratch.t6_{uuid.uuid4().hex[:10]}"

    def url(self, account):
        user, password = (
            (self.cfg["admin"], ADMIN_PW) if account == "admin" else (account, f"{account}_pw")
        )
        return URL.create(
            self.cfg["drivername"],
            username=user,
            password=password,
            host=HOST,
            port=self.cfg["port"],
            database=self.cfg["database"],
        )

    def admin_run(self, sql, params=None):
        eng = sa.create_engine(self.url("admin"), poolclass=NullPool)
        try:
            with eng.begin() as conn:
                result = conn.execute(text(sql), params or {})
                return [tuple(r) for r in result] if result.returns_rows else None
        finally:
            eng.dispose()

    def verify(self, sql, params=None):
        """Rows as seen by a brand-new admin connection."""
        return self.admin_run(sql, params)

    def profile(self, account, read_only=False):
        raw = {
            "dialect": self.dialect,
            "host": HOST,
            "port": self.cfg["port"],
            "database": self.cfg["database"],
            "username": account,
            "connect_timeout_s": 5,
            "read_only": read_only,
        }
        return profiles.validate_profile(f"{self.key}_{account}", raw), f"{account}_pw"

    @contextlib.contextmanager
    def conn(self, account, read_only=False):
        prof, password = self.profile(account, read_only)
        with engine.connection(prof, password) as connection:
            yield connection

    def seed(self):
        self.admin_run(f"DELETE FROM {self.table}")
        for i in range(1, 6):
            self.admin_run(
                f"INSERT INTO {self.table} (k, s, n) VALUES (:k, :s, :n)",
                {"k": f"k{i}", "s": f"s{i}", "n": i},
            )

    def rows(self):
        return self.verify(f"SELECT k, s, n FROM {self.table} ORDER BY n")

    def fail(self, call):
        """Run ``call`` the way the module will: translate whatever is raised with ``errors.raise_for``."""
        try:
            call()
        except Exception as exc:  # pylint: disable=broad-except
            errors.raise_for(exc, self.dialect)
        raise AssertionError("the statement did not fail")  # pragma: no cover


def _reachable(key):
    db = Db(key)
    try:
        db.admin_run("SELECT 1")
        for account in ("ro", "rw"):
            eng = sa.create_engine(db.url(account), poolclass=NullPool)
            try:
                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
            finally:
                eng.dispose()
        if db.admin_run("SELECT count(*) FROM scratch.orders")[0][0] < 2:
            raise RuntimeError("scratch.orders is not seeded")
    except Exception as exc:  # pylint: disable=broad-except
        return (
            db,
            f"{key} not usable at {HOST}:{db.cfg['port']} ({type(exc).__name__}); run tests-support/up.sh auto",
        )
    return db, None


@pytest.fixture(scope="module", params=DIALECTS)
def db(request):
    server, reason = _reachable(request.param)
    if reason:
        pytest.skip(reason)
    server.admin_run(
        f"CREATE TABLE {server.table} (k varchar(20) PRIMARY KEY, s varchar(40), n integer)"
    )
    if server.key == "postgresql":
        server.admin_run(f"GRANT SELECT ON {server.table} TO ro")
        server.admin_run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {server.table} TO rw")
    try:
        yield server
    finally:
        server.admin_run(f"DROP TABLE IF EXISTS {server.table}")


@pytest.fixture
def data(db):
    db.seed()
    return db


def query(db, account, sql, params=None, limit=500, max_bytes=1 << 20, read_only=False):
    with db.conn(account, read_only) as conn:
        return runner.run_query(conn, sql, params, limit=limit, max_bytes=max_bytes)


def execute(db, sql, params=None, dry_run=False, account="rw", read_only=False):
    with db.conn(account, read_only) as conn:
        return runner.run_execute(conn, sql, params, dry_run=dry_run, execute_verbs=VERBS)


# --------------------------------------------------------------------------------------------------
# query: typed values, limits
# --------------------------------------------------------------------------------------------------
ORDERS_SQL = "SELECT id, note, amount, created_at, ref, payload, doc, nothing FROM scratch.orders ORDER BY id"


def test_typed_values_round_trip(db):
    out = query(db, "rw", ORDERS_SQL)
    assert out["columns"] == [
        "id",
        "note",
        "amount",
        "created_at",
        "ref",
        "payload",
        "doc",
        "nothing",
    ]
    assert out["row_count"] == 2 and out["truncated"] is False
    first, second = out["rows"][0], out["rows"][1]
    assert first[0] == 1
    assert first[1] == "café € 日"  # Unicode text
    assert first[2] == "12.50"  # Decimal -> str, no float rounding
    assert first[3] == dt.datetime(2026, 10, 29, 12, 34, 56, 789000).isoformat()
    assert first[4] == "123e4567-e89b-12d3-a456-426614174000"  # UUID (PG) / CHAR(36) (MySQL family)
    assert first[5] == {"$bytes_b64": base64.b64encode(bytes.fromhex("deadbeef")).decode()}
    if db.key == "postgresql":
        assert first[6] == {"a": 1, "b": [1, 2]}  # jsonb -> dict
    else:
        assert first[6] == '{"a": 1, "b": [1, 2]}'  # WS 4: JSON comes back as a str
    assert first[7] is None
    assert second == [2, "plain", "0.00", None, None, None, None, None]  # NULLs stay null
    json.dumps(out, allow_nan=False)


def test_binds_with_cast_and_unicode(db):
    cast_int = "CAST(:i AS SIGNED)" if db.dialect == "mysql" else "CAST(:i AS integer)"
    cast_txt = "CAST(:t AS CHAR)" if db.dialect == "mysql" else "CAST(:t AS text)"
    out = query(
        db,
        "rw",
        f"SELECT {cast_int} + 1 AS n, {cast_txt} AS t",
        {"i": 41, "t": "naïve 日本 \U0001f600"},
    )
    assert out["rows"] == [[42, "naïve 日本 \U0001f600"]]


def test_percent_and_json_colon_escape_work_through_text(db):
    text_type = "CHAR" if db.dialect == "mysql" else "text"
    out = query(
        db,
        "rw",
        f"SELECT CAST('a%b' AS {text_type}) LIKE '%b%' AS m, '{{\"a\"\\:1}}' AS j",
    )
    assert out["rows"][0][1] == '{"a":1}'
    assert out["rows"][0][0] in (1, True)


def test_postgresql_nan_infinity_and_bigint():
    server, reason = _reachable("postgresql")
    if reason:
        pytest.skip(reason)
    out = query(
        server,
        "rw",
        "SELECT CAST('NaN' AS double precision), CAST('Infinity' AS double precision), "
        "CAST(-1 AS bigint) * 9223372036854775807, CAST(9223372036854775807 AS numeric) * 10",
    )
    assert out["rows"] == [["NaN", "Infinity", -9223372036854775807, "92233720368547758070"]]


def test_limit_and_truncated(data):
    sql = f"SELECT k, n FROM {data.table} ORDER BY n"
    out = query(data, "rw", sql, limit=3)
    assert out["rows"] == [["k1", 1], ["k2", 2], ["k3", 3]]
    assert out["row_count"] == 3 and out["truncated"] is True
    out = query(data, "rw", sql, limit=5)
    assert out["row_count"] == 5 and out["truncated"] is False
    out = query(data, "rw", sql, limit=50)
    assert out["row_count"] == 5 and out["truncated"] is False


def test_empty_result_keeps_columns(data):
    out = query(data, "rw", f"SELECT k, n FROM {data.table} WHERE n > 99")
    assert out["columns"] == ["k", "n"] and not out["rows"] and out["truncated"] is False


def test_statement_without_rows(data):
    sql = "SET @t6 = 1" if data.dialect == "mysql" else "SET TIME ZONE 'UTC'"
    out = query(data, "rw", sql)
    assert out["columns"] == [] and not out["rows"] and out["truncated"] is False


def test_large_value_versus_max_bytes(data):
    cast = "CAST(:big AS CHAR)" if data.dialect == "mysql" else "CAST(:big AS text)"
    out = query(data, "rw", f"SELECT {cast} AS big", {"big": "x" * 5000}, max_bytes=1000)
    assert out["columns"] == ["big"] and not out["rows"]
    assert out["row_count"] == 0 and out["truncated"] is True
    out = query(data, "rw", f"SELECT {cast} AS big", {"big": "x" * 5000}, max_bytes=6000)
    assert out["row_count"] == 1 and out["truncated"] is False


def test_max_bytes_cuts_between_rows(data):
    sql = f"SELECT k, s FROM {data.table} ORDER BY n"
    one = len('["k","s"]') + len('[["k1","s1"]]')
    out = query(data, "rw", sql, max_bytes=one + 2)
    assert out["rows"] == [["k1", "s1"]] and out["truncated"] is True


# --------------------------------------------------------------------------------------------------
# query: rollback and read-only
# --------------------------------------------------------------------------------------------------
def test_query_as_rw_leaves_no_data(data):
    before = data.rows()
    query(data, "rw", f"INSERT INTO {data.table} (k, s, n) VALUES ('q1', 'x', 9)")
    query(data, "rw", f"UPDATE {data.table} SET s = 'changed'")
    query(data, "rw", f"DELETE FROM {data.table} WHERE n < 3")
    assert data.rows() == before


def test_query_as_rw_rolls_back_inside_one_connection(data):
    before = data.rows()
    with data.conn("rw") as conn:
        runner.run_query(conn, f"DELETE FROM {data.table}", None, limit=5, max_bytes=1000)
        seen = runner.run_query(
            conn, f"SELECT count(*) FROM {data.table}", None, limit=5, max_bytes=1000
        )
        assert seen["rows"] == [[5]]
    assert data.rows() == before


def test_query_as_ro_cannot_write_the_database_refuses(data):
    before = data.rows()
    with pytest.raises(CommandExecutionError) as exc:
        data.fail(
            lambda: query(data, "ro", f"INSERT INTO {data.table} (k, s, n) VALUES ('w', 'x', 1)")
        )
    message = str(exc.value)
    assert message.startswith("[E_SQL] ")
    if data.key == "postgresql":
        assert (
            message.startswith("[E_SQL] postgresql 42501: ")
            and "permission denied for table" in message
        )
    else:
        assert message.startswith("[E_SQL] mysql 1142: ") and "denied" in message
    assert data.rows() == before


@pytest.mark.parametrize("statement", ["UPDATE {t} SET s = 'x'", "DELETE FROM {t}"])
def test_query_as_ro_cannot_update_or_delete(data, statement):
    before = data.rows()
    with pytest.raises(CommandExecutionError, match=r"^\[E_SQL\] "):
        data.fail(lambda: query(data, "ro", statement.format(t=data.table)))
    assert data.rows() == before


def test_query_as_ro_can_read(data):
    out = query(data, "ro", f"SELECT count(*) FROM {data.table}")
    assert out["rows"] == [[5]]


def test_write_in_read_only_session_is_e_read_only_with_the_servers_text(data):
    """The (best effort) session guard, not a control: see SECURITY section 2."""
    before = data.rows()
    with pytest.raises(CommandExecutionError) as exc:
        data.fail(
            lambda: query(
                data,
                "rw",
                f"INSERT INTO {data.table} (k, s, n) VALUES ('w', 'x', 1)",
                read_only=True,
            )
        )
    message = str(exc.value)
    if data.key == "postgresql":
        assert message.startswith("[E_READ_ONLY] postgresql 25006: ")
        assert "read-only transaction" in message
    else:
        assert message.startswith("[E_READ_ONLY] mysql 1792: ")
        assert "READ ONLY transaction" in message
    assert data.rows() == before


# --------------------------------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------------------------------
def test_execute_commits_and_is_visible_from_a_second_connection(data):
    out = execute(
        data,
        f"INSERT INTO {data.table} (k, s, n) VALUES (:k, :s, :n)",
        {"k": "e1", "s": "ex", "n": 10},
    )
    assert out["rows_affected"] == 1 and out["committed"] is True and out["dry_run"] is False
    assert ("e1", "ex", 10) in data.rows()
    out = execute(data, f"UPDATE {data.table} SET s = 'upd' WHERE n <= 3")
    assert out["rows_affected"] == 3
    assert [r[1] for r in data.rows()][:3] == ["upd", "upd", "upd"]
    out = execute(data, f"DELETE FROM {data.table} WHERE n > 3")
    assert out["rows_affected"] == 3  # n = 4, 5, 10
    assert len(data.rows()) == 3


def test_execute_dry_run_changes_nothing_but_reports_rows_affected(data):
    before = data.rows()
    out = execute(data, f"UPDATE {data.table} SET s = 'dry'", dry_run=True)
    assert out["rows_affected"] == 5 and out["committed"] is False and out["dry_run"] is True
    out = execute(data, f"DELETE FROM {data.table} WHERE n >= 4", dry_run=True)
    assert out["rows_affected"] == 2
    out = execute(data, f"INSERT INTO {data.table} (k, s, n) VALUES ('d', 'x', 7)", dry_run=True)
    assert out["rows_affected"] == 1
    assert data.rows() == before


def test_execute_unique_violation_rolls_back(data):
    before = data.rows()
    with pytest.raises(CommandExecutionError) as exc:
        data.fail(
            lambda: execute(data, f"INSERT INTO {data.table} (k, s, n) VALUES ('k1', 'x', 99)")
        )
    assert str(exc.value).startswith("[E_CONSTRAINT] ")
    assert data.rows() == before


def test_execute_verb_outside_policy_is_refused_before_the_database(data):
    before = data.rows()
    with pytest.raises(SaltInvocationError) as exc:
        execute(data, f"DROP TABLE {data.table}")
    assert str(exc.value).startswith("[E_POLICY] ")
    assert data.rows() == before
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\] "):
        execute(data, "-- nothing here\n/* nor here */")


def test_execute_with_comments_before_the_verb(data):
    out = execute(data, f"-- bump\n/* t6 */ UPDATE {data.table} SET s = 'c' WHERE n = 1")
    assert out["rows_affected"] == 1 and out["committed"] is True


def test_execute_as_ro_is_refused_by_the_database(data):
    before = data.rows()
    with pytest.raises(CommandExecutionError, match=r"^\[E_SQL\] "):
        data.fail(lambda: execute(data, f"DELETE FROM {data.table}", account="ro"))
    assert data.rows() == before


# --------------------------------------------------------------------------------------------------
# error fidelity through errors.raise_for
# --------------------------------------------------------------------------------------------------
SECRET = "BOUND-SECRET-t6-4f2a"
MARKER = "t6_sql_marker"

SYNTAX = {
    "native": {"postgresql": "42601", "mysql": "1064", "mariadb": "1064"},
    "text": {
        "postgresql": "syntax error at or near",
        "mysql": "You have an error in your SQL syntax",
        "mariadb": "You have an error in your SQL syntax",
    },
}
MISSING_TABLE = {
    "native": {"postgresql": "42P01", "mysql": "1146", "mariadb": "1146"},
    "text": {
        "postgresql": 'relation "scratch.t6_missing_table" does not exist',
        "mysql": "t6_missing_table' doesn't exist",
        "mariadb": "t6_missing_table' doesn't exist",
    },
}
MISSING_COLUMN = {
    "native": {"postgresql": "42703", "mysql": "1054", "mariadb": "1054"},
    "text": {
        "postgresql": 'column "t6_no_such_col" ',
        "mysql": "Unknown column 't6_no_such_col'",
        "mariadb": "Unknown column 't6_no_such_col'",
    },
}
UNIQUE = {
    "native": {"postgresql": "23505", "mysql": "1062", "mariadb": "1062"},
    "text": {
        "postgresql": "duplicate key value violates unique constraint",
        # lib/errors masks the row value the server echoes (Duplicate entry 'k1' -> '...')
        "mysql": "Duplicate entry '...' for key",
        "mariadb": "Duplicate entry '...' for key",
    },
}

#: (id, path, SQL template, expected code, expectations). {t} is the scratch table, {m} a marker that must never
#: appear in a message, :secret a bound value that must never appear in one.
ERROR_CASES = [
    ("query-syntax", "query", "/* {m} */ selec 1", "E_SQL", SYNTAX),
    (
        "query-missing-table",
        "query",
        "SELECT * FROM scratch.t6_missing_table WHERE s = :secret /* {m} */",
        "E_SQL",
        MISSING_TABLE,
    ),
    (
        "query-missing-column",
        "query",
        "SELECT t6_no_such_col FROM {t} WHERE s = :secret /* {m} */",
        "E_SQL",
        MISSING_COLUMN,
    ),
    ("execute-syntax", "execute", "/* {m} */ insert into {t} valuess (1)", "E_SQL", SYNTAX),
    (
        "execute-missing-table",
        "execute",
        "UPDATE scratch.t6_missing_table SET s = :secret /* {m} */",
        "E_SQL",
        MISSING_TABLE,
    ),
    (
        "execute-missing-column",
        "execute",
        "UPDATE {t} SET t6_no_such_col = :secret /* {m} */",
        "E_SQL",
        MISSING_COLUMN,
    ),
    (
        "execute-unique-violation",
        "execute",
        "INSERT INTO {t} (k, s, n) VALUES ('k1', :secret, 1) /* {m} */",
        "E_CONSTRAINT",
        UNIQUE,
    ),
]


@pytest.mark.parametrize(
    "path,template,code,expect", [c[1:] for c in ERROR_CASES], ids=[c[0] for c in ERROR_CASES]
)
def test_failing_statement_error_fidelity(data, path, template, code, expect):
    sql = template.format(t=data.table, m=MARKER)
    params = {"secret": SECRET} if ":secret" in sql else None

    def run():
        if path == "query":
            return query(data, "rw", sql, params)
        return execute(data, sql, params)

    before = data.rows()
    with pytest.raises(CommandExecutionError) as exc:
        data.fail(run)
    message = str(exc.value)
    native = expect["native"][data.key]
    assert message.startswith(f"[{code}] {data.dialect} {native}: "), message
    assert expect["text"][data.key] in message, message
    assert MARKER not in message
    assert SECRET not in message
    assert "[SQL:" not in message and "[parameters:" not in message
    assert "rw_pw" not in message and "://" not in message
    assert exc.value.__cause__ is None
    assert data.rows() == before


def test_unbound_parameter_is_option_invalid_without_values(data):
    with pytest.raises(CommandExecutionError) as exc:
        data.fail(
            lambda: query(data, "rw", f"SELECT CAST(:needed AS CHAR) FROM {data.table} -- {MARKER}")
        )
    message = str(exc.value)
    assert message.startswith("[E_OPTION_INVALID] ")
    assert MARKER not in message


def test_double_colon_cast_trap_is_a_real_server_syntax_error(data):
    """`:a::int` is not a bind (test_text_binds); the server then reports a syntax error, and we pass it on."""
    with pytest.raises(CommandExecutionError) as exc:
        data.fail(lambda: query(data, "rw", "SELECT :a::int", {"a": SECRET}))
    assert str(exc.value).startswith("[E_SQL] ")
    assert SECRET not in str(exc.value)
