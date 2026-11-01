"""The pg8000 specifics of ``lib/dialects.py`` (SPEC decision D15, docs/WIRE_SHAPES.md section 16).

These tests use the real pg8000 classes (a ``tests`` extra) with a stand-in connection, so no server is needed. What
a real server answers is covered by ``tests/functional/test_pg8000_functional.py``.
"""

import datetime
import decimal
import uuid

import pytest
from salt.exceptions import CommandExecutionError
from sqlalchemy import create_engine

from saltext.sqlx.lib import dialects

pg8000 = pytest.importorskip("pg8000")


class FakeContext:
    def __init__(self, rows=None):
        self.rows = rows


class FakeConn:
    """The few members of ``pg8000.core.CoreConnection`` that the cursor touches."""

    autocommit = False

    def __init__(self, failed=False, rows=None):
        self._in_transaction = False
        self._transaction_status = b"E" if failed else b"I"
        self._sock = object()
        self.calls = []
        self.rows = rows

    def execute_simple(self, statement):
        self.calls.append(("simple", statement))
        self._in_transaction = True

    def execute_unnamed(self, statement, vals=(), oids=(), stream=None):
        assert stream is None
        self.calls.append(("unnamed", statement, vals, oids))
        return FakeContext(self.rows)


@pytest.fixture
def cursor_class():
    return dialects._pg8000_cursor_class()  # pylint: disable=protected-access


def test_every_statement_goes_through_the_extended_protocol(cursor_class):
    conn = FakeConn(rows=[[1]])
    cur = cursor_class(conn)
    cur.execute(
        "SELECT 1; SELECT 2"
    )  # no arguments: pg8000's own cursor would use the simple protocol here
    assert conn.calls == [
        ("simple", "begin transaction"),
        ("unnamed", "SELECT 1; SELECT 2", (), ()),
    ]
    assert list(cur._row_iter) == [[1]]  # pylint: disable=protected-access


def test_percent_signs_reach_the_server_untouched(cursor_class):
    conn = FakeConn()
    sql = "SELECT 100 % 7, 'a%b' LIKE '%b', $1"
    cursor_class(conn).execute(sql, (5,))
    assert conn.calls[-1][1] == sql  # pg8000's format scanner would have raised on the lone percent


def test_second_statement_in_a_transaction_does_not_begin_again(cursor_class):
    conn = FakeConn()
    cur = cursor_class(conn)
    cur.execute("SELECT 1")
    cur.execute("SELECT 2")
    assert [c[0] for c in conn.calls].count("simple") == 1


def test_close_of_a_server_side_cursor_in_an_aborted_transaction_is_skipped(cursor_class):
    conn = FakeConn(failed=True)
    cursor_class(conn).execute('CLOSE "c_1"')
    assert not [c for c in conn.calls if c[0] == "unnamed"]
    ok = FakeConn()
    cursor_class(ok).execute('CLOSE "c_1"')
    assert ok.calls[-1][1] == 'CLOSE "c_1"'


def test_closed_cursor_and_connection_are_interface_errors(cursor_class):
    cur = cursor_class(FakeConn())
    cur._c = None  # pylint: disable=protected-access
    with pytest.raises(pg8000.dbapi.InterfaceError, match="Cursor closed"):
        cur.execute("SELECT 1")
    conn = FakeConn()
    conn._sock = None  # pylint: disable=protected-access

    def gone(*args, **kw):
        raise AttributeError("'NoneType' object has no attribute 'write'")

    conn.execute_unnamed = gone
    with pytest.raises(pg8000.dbapi.InterfaceError, match="connection is closed"):
        cursor_class(conn).execute("SELECT 1")


@pytest.mark.parametrize(
    "value,oid",
    [
        (True, 16),
        (7, 21),
        (40_000, 23),
        (2**40, 20),
        (2**70, 1700),
        (-(2**31) - 1, 20),
        (1.5, 701),
        (decimal.Decimal("1.5"), 1700),
        (b"x", 17),
        (bytearray(b"x"), 17),
        (datetime.date(2020, 1, 2), 1082),
        (datetime.datetime(2020, 1, 2), 1114),
        (datetime.datetime(2020, 1, 2, tzinfo=datetime.timezone.utc), 1184),
        (datetime.time(1, 2), 1083),
        (datetime.time(1, 2, tzinfo=datetime.timezone.utc), 1266),
        (datetime.timedelta(1), 1186),
        (uuid.UUID(int=1), 2950),
        ("text", 0),
        (None, 0),
        ([1, 2], 0),
        ({"a": 1}, 0),
    ],
)
def test_bound_scalars_declare_the_type_psycopg_declares(value, oid):
    assert dialects._pg8000_oid(value) == oid  # pylint: disable=protected-access


def test_declared_oids_are_sent_with_the_statement(cursor_class):
    conn = FakeConn()
    cursor_class(conn).execute("SELECT $1, $2, $3", (1, "x", None))
    assert conn.calls[-1][2:] == ((1, "x", None), (21, 0, 0))


# -- the once-per-process proof ----------------------------------------------------------------------------------------------
class ProofConn:
    def __init__(self, refuse):
        self.refuse = refuse
        self.closed = False
        self.rolled_back = False

    def cursor(self):
        return self

    def execute(self, sql):
        assert sql == "SELECT 1; SELECT 2"
        if self.refuse:
            raise pg8000.dbapi.ProgrammingError(
                {"C": "42601", "M": "cannot insert multiple commands"}
            )

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture
def unproven(monkeypatch):
    monkeypatch.setattr(dialects, "_PG8000_PROVEN", False)


@pytest.mark.usefixtures("unproven")
def test_a_driver_that_refuses_the_second_statement_is_proven_once():
    conn = ProofConn(refuse=True)
    dialects._prove(conn)  # pylint: disable=protected-access
    assert conn.rolled_back and not conn.closed
    dialects._prove(
        object()
    )  # proven: no further round trip, nothing touched  # pylint: disable=protected-access


@pytest.mark.usefixtures("unproven")
@pytest.mark.parametrize("wrong", ["accepts", "other-error"])
def test_a_driver_that_runs_two_statements_fails_closed(wrong):
    conn = ProofConn(refuse=False)
    if wrong == "other-error":
        conn.execute = lambda sql: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(CommandExecutionError) as exc:
        dialects._prove(conn)  # pylint: disable=protected-access
    assert str(exc.value).startswith("[E_DRIVER_MISSING] postgresql driver pg8000")
    assert "saltext-sqlx[postgres]" in str(exc.value)
    assert conn.closed
    assert dialects._PG8000_PROVEN is False  # pylint: disable=protected-access


def test_driver_without_the_expected_internals_is_refused(monkeypatch):
    monkeypatch.setattr(dialects, "_PG8000_CURSOR", None)
    real = dialects.importlib.import_module

    def fake(name):
        module = real(name)
        if name == "pg8000.core":
            return type(
                "core",
                (),
                {"CoreConnection": type("CoreConnection", (), {}), "IN_FAILED_TRANSACTION": b"E"},
            )
        return module

    monkeypatch.setattr(dialects.importlib, "import_module", fake)
    with pytest.raises(CommandExecutionError) as exc:
        dialects._pg8000_cursor_class()  # pylint: disable=protected-access
    assert str(exc.value).startswith("[E_DRIVER_MISSING]")


# -- engine hooks --------------------------------------------------------------------------------------------------------------
def profile(**kw):
    prof = {
        "name": "p",
        "dialect": "postgresql",
        "driver": "pg8000",
        "host": "h",
        "port": 5432,
        "database": "d",
        "username": "u",
        "query": {},
        "connect_args": {},
        "read_only": True,
        "statement_timeout_ms": 2500,
        "connect_timeout_s": 3,
    }
    prof.update(kw)
    return prof


def test_configure_engine_registers_the_connect_and_do_connect_hooks():
    eng = create_engine("sqlite://")
    dialects.adapter("postgresql").configure_engine(eng, profile())
    assert len(eng.dialect.dispatch.do_connect) == 1
    assert len(eng.pool.dispatch.connect) >= 1


def test_do_connect_turns_the_scram_attribute_error_into_the_no_password_message():
    eng = create_engine("sqlite://")
    dialects.adapter("postgresql").configure_engine(eng, profile())
    hook = list(eng.dialect.dispatch.do_connect)[0]

    class Dialect:
        @staticmethod
        def connect(*args, **kw):
            raise AttributeError("'NoneType' object has no attribute 'decode'")

    with pytest.raises(pg8000.dbapi.InterfaceError, match="no password was provided"):
        hook(Dialect, None, (), {"password": None})
    with pytest.raises(
        AttributeError
    ):  # with a password the error is not about the password: let it be internal
        hook(Dialect, None, (), {"password": "x"})


def test_statement_timeout_backstop_is_one_second_above_the_server_limit():
    adapter = dialects.adapter("postgresql")
    assert adapter.READ_TIMEOUT_MARGIN_S == 1.0
    assert adapter.connect_args(profile(connect_timeout_s=4))["timeout"] == 4
