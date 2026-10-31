"""SQL Server rules in saltext.sqlx.lib.runner (task T1b, docs/WIRE_SHAPES.md section 14).

Observed on SQL Server 2022 with pymssql 2.4.6: a string with several statements runs as one batch (``a; b`` and even
``a b`` without a separator), a ``COMMIT`` inside the batch survives the module's rollback, and pymssql interpolates
bound values into the statement text, where a NUL character cuts the statement short.
"""

import types

import pytest
from salt.exceptions import SaltInvocationError

from saltext.sqlx.lib import runner


class Cursor:
    def __init__(self, more=False):
        self.more = more

    def nextset(self):
        more, self.more = self.more, False
        return True if more else None


class Result:
    def __init__(self, rows=((1,),), more=False, returns_rows=True):
        self.returns_rows = returns_rows
        self._rows = list(rows)
        self.cursor = Cursor(more)
        self.rowcount = 1

    def keys(self):
        return ["c"]

    def fetchmany(self, size):
        return self._rows[:size]

    def close(self):
        pass


class Conn:
    def __init__(self, name="mssql", result=None):
        self.dialect = types.SimpleNamespace(name=name)
        self.result = result or Result()
        self.events = []

    def execute(self, _clause, _params=None, **_kw):
        self.events.append("execute")
        return self.result

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def invalidate(self):
        self.events.append("invalidate")


SECOND = [
    "insert into t values (1); insert into t values (2)",
    "insert into t values (1);insert into t values (2)",
    "select 1; commit",
    "update t set v = 1 ; -- c\n delete from t",
    "select 'a;b' as x; select 2",
    "select [a;b] from t; select 2",
]
SINGLE = [
    "select 1",
    "select 1;",
    "select 1 ;  ",
    "select 1; -- trailing comment",
    "select 1; /* trailing */ ;",
    ";with a as (select 1 as x) select x from a",
    "select 'a;b' as x",
    "select N'it''s; fine' as x",
    'select "a;b" from t',
    "select [a;b], [x]]y;] from t",
    "select 1 -- ; select 2",
    "select 1 /* ; select 2 /* nested ; */ still ; */",
]


@pytest.mark.parametrize("sql", SECOND)
@pytest.mark.parametrize("fn", ["query", "execute"])
def test_mssql_refuses_a_second_statement_before_sending(sql, fn):
    conn = Conn()
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\] .*more than one statement"):
        if fn == "query":
            runner.run_query(conn, sql, None, limit=10, max_bytes=1000)
        else:
            runner.run_execute(
                conn, sql, None, dry_run=True, execute_verbs=["INSERT", "UPDATE", "SELECT"]
            )
    assert "execute" not in conn.events


@pytest.mark.parametrize("sql", SINGLE)
def test_mssql_single_statement_passes(sql):
    conn = Conn()
    assert runner.run_query(conn, sql, None, limit=10, max_bytes=1000)["rows"] == [[1]]


@pytest.mark.parametrize("sql", SECOND)
def test_other_dialects_leave_it_to_the_server(sql):
    # PostgreSQL (extended protocol), MySQL/MariaDB (no multi-statements flag) and Oracle refuse it themselves
    conn = Conn(name="postgresql")
    runner.run_query(conn, sql, None, limit=10, max_bytes=1000)
    assert conn.events[0] == "execute"


def test_mssql_second_result_set_without_separator_is_refused_and_rolled_back():
    conn = Conn(result=Result(more=True))
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\] .*more than one statement"):
        runner.run_query(conn, "select 1 select 2", None, limit=10, max_bytes=1000)
    assert conn.events == ["execute", "rollback"]


def test_mssql_execute_second_result_set_is_rolled_back_not_committed():
    conn = Conn(result=Result(more=True, returns_rows=False))
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        runner.run_execute(
            conn, "update t set v = 1 select 2", None, dry_run=False, execute_verbs=["UPDATE"]
        )
    assert "commit" not in conn.events and conn.events[-1] == "rollback"


@pytest.mark.parametrize("params", [{"x": "a\x00b"}, {"x": "ok", "y": "\x00"}])
def test_mssql_refuses_nul_in_a_bound_string(params):
    conn = Conn()
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\] .*NUL"):
        runner.run_query(conn, "select :x", params, limit=10, max_bytes=1000)
    assert "execute" not in conn.events
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\] .*NUL"):
        runner.run_execute(
            conn, "insert into t values (:x)", params, dry_run=True, execute_verbs=["INSERT"]
        )
    assert "execute" not in conn.events


def test_nul_in_a_bound_string_is_data_elsewhere():
    conn = Conn(name="oracle")
    runner.run_query(conn, "select :x from dual", {"x": "a\x00b"}, limit=10, max_bytes=1000)
    assert conn.events == ["execute", "rollback"]
