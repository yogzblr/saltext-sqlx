"""Unit tests for saltext.sqlx.lib.runner (TEST_PLAN section 3), run on SQLite.

A file database is used so that a SECOND connection, on a second engine, can prove what was and was not committed.
"""

import json

import pytest
import sqlalchemy as sa
from salt.exceptions import SaltInvocationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from saltext.sqlx.lib import runner

VERBS = ("insert", "update", "delete")


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'runner.db'}"
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        conn.execute(text("INSERT INTO t (id, v) VALUES (1, 'a'), (2, 'b'), (3, 'c')"))
    eng.dispose()
    return url


@pytest.fixture
def conn(db_url):
    eng = sa.create_engine(db_url)
    with eng.connect() as connection:
        yield connection
    eng.dispose()


@pytest.fixture
def other(db_url):
    """A second, independent connection used to verify what really reached the database."""
    eng = sa.create_engine(db_url)
    with eng.connect() as connection:
        yield connection
        connection.rollback()
    eng.dispose()


def count(connection):
    return connection.execute(text("SELECT count(*) FROM t")).scalar()


def values(connection):
    return [r[0] for r in connection.execute(text("SELECT v FROM t ORDER BY id"))]


class FakeResult:
    returns_rows = True

    def __init__(self, rows=(), keys=("c",), rowcount=0):
        self._rows = list(rows)
        self._keys = keys
        self.rowcount = rowcount
        self.fetch_args = []
        self.closed = False

    def keys(self):
        return self._keys

    def fetchmany(self, size):
        self.fetch_args.append(size)
        return self._rows[:size]

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, result=None, fail_execute=None, fail_commit=None, fail_rollback=None):
        self.result = result or FakeResult()
        self.fail_execute = fail_execute
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.events = []

    def execute(self, clause, params=None):
        self.events.append("execute")
        if self.fail_execute:
            raise self.fail_execute
        return self.result

    def commit(self):
        self.events.append("commit")
        if self.fail_commit:
            raise self.fail_commit

    def rollback(self):
        self.events.append("rollback")
        if self.fail_rollback:
            raise self.fail_rollback


# --------------------------------------------------------------------------------------------------
# run_query
# --------------------------------------------------------------------------------------------------
def test_query_returns_columns_rows_and_timing(conn):
    out = runner.run_query(
        conn, "SELECT id, v FROM t ORDER BY id", None, limit=10, max_bytes=100_000
    )
    assert out["columns"] == ["id", "v"]
    assert out["rows"] == [[1, "a"], [2, "b"], [3, "c"]]
    assert out["row_count"] == 3
    assert out["truncated"] is False
    assert isinstance(out["elapsed_ms"], int) and out["elapsed_ms"] >= 0
    assert set(out) == {"columns", "rows", "row_count", "truncated", "elapsed_ms"}
    json.dumps(out, allow_nan=False)


def test_query_binds_params_and_never_interpolates(conn):
    evil = "x'; DROP TABLE t; --"
    out = runner.run_query(conn, "SELECT :a + 1 AS n, :s AS s", {"a": 41, "s": evil}, limit=5, max_bytes=10_000)
    assert out["rows"] == [[42, evil]]
    assert count(conn) == 3


def test_query_fetches_limit_plus_one(db_url):
    fake = FakeConn(FakeResult(rows=[(i,) for i in range(10)]))
    out = runner.run_query(fake, "select 1", None, limit=4, max_bytes=100_000)
    assert fake.result.fetch_args == [5]
    assert out["row_count"] == 4 and out["truncated"] is True
    assert fake.result.closed is True


def test_query_limit_truncation(conn):
    out = runner.run_query(conn, "SELECT id FROM t ORDER BY id", None, limit=2, max_bytes=100_000)
    assert out["rows"] == [[1], [2]]
    assert out["row_count"] == 2
    assert out["truncated"] is True


def test_query_exactly_limit_rows_is_not_truncated(conn):
    out = runner.run_query(conn, "SELECT id FROM t ORDER BY id", None, limit=3, max_bytes=100_000)
    assert out["row_count"] == 3
    assert out["truncated"] is False


def test_query_byte_cap_truncation(conn):
    full = runner.run_query(conn, "SELECT id, v FROM t ORDER BY id", None, limit=10, max_bytes=100_000)
    size = len(json.dumps(full["columns"], separators=(",", ":"))) + len(
        json.dumps(full["rows"][:2], separators=(",", ":"))
    )
    out = runner.run_query(conn, "SELECT id, v FROM t ORDER BY id", None, limit=10, max_bytes=size)
    assert out["rows"] == [[1, "a"], [2, "b"]]
    assert out["truncated"] is True


def test_query_byte_cap_too_small_for_first_row(conn):
    out = runner.run_query(conn, "SELECT id, v FROM t ORDER BY id", None, limit=10, max_bytes=5)
    assert out["rows"] == [] and out["row_count"] == 0
    assert out["truncated"] is True


def test_query_statement_without_rows(conn):
    out = runner.run_query(conn, "INSERT INTO t (id, v) VALUES (9, 'z')", None, limit=5, max_bytes=1000)
    assert out["columns"] == [] and out["rows"] == []
    assert out["row_count"] == 0 and out["truncated"] is False


def test_query_select_with_no_matching_rows_keeps_columns(conn):
    out = runner.run_query(conn, "SELECT id, v FROM t WHERE id > 99", None, limit=5, max_bytes=1000)
    assert out["columns"] == ["id", "v"] and out["rows"] == []
    assert out["truncated"] is False


def test_query_write_is_rolled_back_seen_from_second_connection(conn, other):
    runner.run_query(conn, "INSERT INTO t (id, v) VALUES (10, 'new')", None, limit=5, max_bytes=1000)
    runner.run_query(conn, "UPDATE t SET v = 'changed'", None, limit=5, max_bytes=1000)
    runner.run_query(conn, "DELETE FROM t WHERE id = 1", None, limit=5, max_bytes=1000)
    assert count(other) == 3
    assert values(other) == ["a", "b", "c"]
    # and the query connection itself sees the original data, because the transaction ended
    assert values(conn) == ["a", "b", "c"]


def test_query_rolls_back_even_when_the_statement_fails(conn, other):
    with pytest.raises(OperationalError) as exc:
        runner.run_query(conn, "SELECT * FROM no_such_table", None, limit=5, max_bytes=1000)
    assert "no such table" in str(exc.value.orig)
    assert not conn.in_transaction()
    assert count(other) == 3


def test_query_bind_error_propagates_unchanged(conn):
    with pytest.raises(sa.exc.StatementError):
        runner.run_query(conn, "SELECT :needed", {}, limit=5, max_bytes=1000)
    assert not conn.in_transaction()


def test_query_exception_is_the_original_object():
    boom = RuntimeError("boom")
    fake = FakeConn(fail_execute=boom)
    with pytest.raises(RuntimeError) as exc:
        runner.run_query(fake, "select 1", None, limit=1, max_bytes=10)
    assert exc.value is boom
    assert fake.events == ["execute", "rollback"]


def test_query_never_commits():
    fake = FakeConn(FakeResult(rows=[(1,)]), fail_commit=AssertionError("query committed"))
    runner.run_query(fake, "select 1", None, limit=5, max_bytes=100)
    assert fake.events == ["execute", "rollback"]


def test_query_rollback_failure_does_not_hide_the_result_or_the_error():
    fake = FakeConn(FakeResult(rows=[(1,)]), fail_rollback=RuntimeError("rollback broke"))
    out = runner.run_query(fake, "select 1", None, limit=5, max_bytes=100)
    assert out["rows"] == [[1]]
    boom = ValueError("original")
    fake = FakeConn(fail_execute=boom, fail_rollback=RuntimeError("rollback broke"))
    with pytest.raises(ValueError) as exc:
        runner.run_query(fake, "select 1", None, limit=5, max_bytes=100)
    assert exc.value is boom


# --------------------------------------------------------------------------------------------------
# run_execute
# --------------------------------------------------------------------------------------------------
def test_execute_commits_and_is_visible_from_second_connection(conn, other):
    out = runner.run_execute(
        conn, "INSERT INTO t (id, v) VALUES (:i, :v)", {"i": 4, "v": "d"}, dry_run=False, execute_verbs=VERBS
    )
    assert set(out) == {"rows_affected", "committed", "dry_run", "elapsed_ms"}
    assert out["rows_affected"] == 1
    assert out["committed"] is True and out["dry_run"] is False
    assert isinstance(out["elapsed_ms"], int)
    assert values(other) == ["a", "b", "c", "d"]
    json.dumps(out, allow_nan=False)


def test_execute_update_and_delete_report_rows_affected(conn, other):
    out = runner.run_execute(conn, "UPDATE t SET v = 'x' WHERE id < 3", None, dry_run=False, execute_verbs=VERBS)
    assert out["rows_affected"] == 2
    out = runner.run_execute(conn, "DELETE FROM t", None, dry_run=False, execute_verbs=VERBS)
    assert out["rows_affected"] == 3
    assert count(other) == 0


def test_execute_dry_run_changes_nothing_but_reports_rows_affected(conn, other):
    out = runner.run_execute(conn, "UPDATE t SET v = 'x'", None, dry_run=True, execute_verbs=VERBS)
    assert out["rows_affected"] == 3
    assert out["committed"] is False and out["dry_run"] is True
    assert values(other) == ["a", "b", "c"]
    assert not conn.in_transaction()


def test_execute_dry_run_never_calls_commit():
    fake = FakeConn(FakeResult(rowcount=7), fail_commit=AssertionError("dry run committed"))
    out = runner.run_execute(fake, "delete from t", None, dry_run=True, execute_verbs=VERBS)
    assert out["rows_affected"] == 7
    assert fake.events == ["execute", "rollback"]


def test_execute_error_rolls_back_and_reraises_original(conn, other):
    with pytest.raises(OperationalError):
        runner.run_execute(conn, "INSERT INTO nope VALUES (1)", None, dry_run=False, execute_verbs=VERBS)
    assert not conn.in_transaction()
    assert count(other) == 3
    boom = RuntimeError("x")
    fake = FakeConn(fail_execute=boom)
    with pytest.raises(RuntimeError) as exc:
        runner.run_execute(fake, "delete from t", None, dry_run=False, execute_verbs=VERBS)
    assert exc.value is boom
    assert fake.events == ["execute", "rollback"]


def test_execute_unique_violation_leaves_data_unchanged(conn, other):
    with pytest.raises(sa.exc.IntegrityError):
        runner.run_execute(conn, "INSERT INTO t (id, v) VALUES (1, 'dup')", None, dry_run=False, execute_verbs=VERBS)
    assert values(other) == ["a", "b", "c"]


def test_execute_commit_failure_rolls_back_and_reraises():
    boom = RuntimeError("commit failed")
    fake = FakeConn(FakeResult(rowcount=1), fail_commit=boom)
    with pytest.raises(RuntimeError) as exc:
        runner.run_execute(fake, "delete from t", None, dry_run=False, execute_verbs=VERBS)
    assert exc.value is boom
    assert fake.events == ["execute", "commit", "rollback"]


def test_execute_rollback_failure_does_not_hide_the_original_error():
    boom = ValueError("original")
    fake = FakeConn(fail_execute=boom, fail_rollback=RuntimeError("rollback broke"))
    with pytest.raises(ValueError) as exc:
        runner.run_execute(fake, "delete from t", None, dry_run=False, execute_verbs=VERBS)
    assert exc.value is boom


@pytest.mark.parametrize("dry_run", [None, 0, 1, "false", "true", []])
def test_execute_dry_run_must_be_a_real_bool(dry_run):
    fake = FakeConn()
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\]"):
        runner.run_execute(fake, "delete from t", None, dry_run=dry_run, execute_verbs=VERBS)
    assert fake.events == []


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "insert into t values (1)",
        "InSeRt into t values (1)",
        "   \n\t insert into t values (1)",
        "-- note\ninsert into t values (1)",
        "-- one\n-- two\r\n  insert into t values (1)",
        "/* note */ insert into t values (1)",
        "/* a */ /* b */\n-- c\n /* d */insert into t values (1)",
        "/* multi\nline */INSERT into t values (1)",
        "insert\ninto t values (1)",
        "insert",
    ],
)
def test_verb_filter_allows(sql):
    fake = FakeConn(FakeResult(rowcount=1))
    runner.run_execute(fake, sql, None, dry_run=False, execute_verbs=VERBS)
    assert fake.events == ["execute", "commit"]


@pytest.mark.parametrize(
    "sql,verb",
    [
        ("DROP TABLE t", "DROP"),
        ("drop table t", "drop"),
        ("  -- insert\n DROP TABLE t", "DROP"),
        ("/* insert */ truncate t", "truncate"),
        ("-- delete\n/* update */ CREATE TABLE x (a int)", "CREATE"),
        ("SELECT 1", "SELECT"),
        ("insertinto t values (1)", "insertinto"),
        ("grant all on t to x", "grant"),
    ],
)
def test_verb_filter_refuses_verb_outside_policy(sql, verb):
    fake = FakeConn()
    with pytest.raises(SaltInvocationError) as exc:
        runner.run_execute(fake, sql, None, dry_run=False, execute_verbs=VERBS)
    message = str(exc.value)
    assert message.startswith("[E_POLICY] ")
    assert verb in message
    assert "insert, update, delete" in message
    assert fake.events == []


@pytest.mark.parametrize("sql", ["(select 1)", "; drop table t", "1", "'x'", "$$ x $$", "é", "@x := 1"])
def test_verb_filter_refuses_statement_not_starting_with_a_keyword(sql):
    fake = FakeConn()
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\] ") as exc:
        runner.run_execute(fake, sql, None, dry_run=False, execute_verbs=VERBS)
    assert sql.strip() not in str(exc.value) or len(sql.strip()) <= 1
    assert fake.events == []


@pytest.mark.parametrize(
    "sql",
    ["", "   ", "\n\t", "-- only a comment", "-- c\n   ", "/* only */", "/* a */ -- b", "/* unterminated insert"],
)
def test_verb_filter_empty_or_comment_only_is_option_invalid(sql):
    fake = FakeConn()
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\] "):
        runner.run_execute(fake, sql, None, dry_run=False, execute_verbs=VERBS)
    assert fake.events == []


def test_verb_filter_message_does_not_leak_the_statement_body():
    fake = FakeConn()
    with pytest.raises(SaltInvocationError) as exc:
        runner.run_execute(
            fake, "DROP TABLE t -- TOPSECRET-VALUE", {"p": "BOUND-SECRET"}, dry_run=False, execute_verbs=VERBS
        )
    assert "TOPSECRET" not in str(exc.value)
    assert "BOUND-SECRET" not in str(exc.value)
    assert " t" not in str(exc.value).split("DROP", 1)[1].split("insert")[0]


@pytest.mark.parametrize("verbs", [["INSERT", "Update"], ("insert", "update"), {"INSERT", "UPDATE"}])
def test_execute_verbs_are_case_insensitive_and_any_collection(verbs):
    fake = FakeConn(FakeResult(rowcount=1))
    runner.run_execute(fake, "Update t set v = 1", None, dry_run=True, execute_verbs=verbs)
    assert fake.events == ["execute", "rollback"]


def test_empty_execute_verbs_refuses_everything():
    fake = FakeConn()
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\] "):
        runner.run_execute(fake, "insert into t values (1)", None, dry_run=False, execute_verbs=())
    assert fake.events == []


def test_execute_params_default_to_empty_and_negative_rowcount_is_kept():
    fake = FakeConn(FakeResult(rowcount=-1))
    out = runner.run_execute(fake, "update t set v = 1", None, dry_run=False, execute_verbs=VERBS)
    assert out["rows_affected"] == -1
