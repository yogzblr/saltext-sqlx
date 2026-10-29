"""Unit tests for saltext.sqlx.modules.sqlx_mod (TEST_PLAN ``test_module.py``).

``__salt__``, ``__opts__`` and the database layer (``engine`` connection, ``runner``) are injected fakes: no Salt
daemon and no database. Real servers are covered by ``tests/functional/test_sqlx_module_functional.py``.
"""

# pylint: disable=unused-argument
import contextlib
import hashlib
import importlib.util
import json
import logging
import sys
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod

PG_PASSWORD = "pg-Secret:p@ss%word-Zq9"
RW_PASSWORD = "rw-Secret-literal-Xk4"
SQL = "select secret_column_name from secret_table_name where x = :x"
SQL_DIGEST = hashlib.sha256(SQL.encode()).hexdigest()[:12]

CONFIG = {
    "profiles": {
        "pg_ro": {
            "dialect": "postgresql",
            "host": "db1",
            "port": 5432,
            "database": "orders",
            "username": "ro",
            "password": "sdb://vault/pg_ro",
        },
        "pg_rw": {
            "dialect": "postgresql",
            "host": "db1",
            "port": 5432,
            "database": "orders",
            "username": "rw",
            "password": RW_PASSWORD,
            "allow_execute": True,
            "read_only": False,
        },
        "my_ro": {
            "dialect": "mysql",
            "host": "db2",
            "port": 3306,
            "database": "scratch",
            "username": "ro",
            "error_detail": True,
        },
    }
}

ROWS = {
    "columns": ["id", "total"],
    "rows": [[1, "12.50"], [2, None]],
    "row_count": 2,
    "truncated": False,
    "elapsed_ms": 3,
}


class Orig(Exception):
    """Driver exception stand-in with a SQLSTATE (psycopg style)."""

    def __init__(self, message, sqlstate):
        super().__init__(message)
        self.sqlstate = sqlstate


def db_error(sqlstate, message, cls=sa.exc.ProgrammingError):
    # SQLAlchemy's own str() embeds the statement and the values; the module must never use it.
    return cls(SQL, {"x": "bound-value-77"}, Orig(message, sqlstate))


class Fake:
    """Records what the module asked the database layer to do."""

    def __init__(self):
        self.connections = []
        self.queries = []
        self.executes = []
        self.pings = []
        self.sdb = []
        self.query_result = dict(ROWS)
        self.query_result_fn = None
        self.query_error = None
        self.execute_error = None
        self.connect_error = None

    @contextmanager
    def connection(self, profile, password, timeout_ms=None, read_only=None):
        if self.connect_error is not None:
            raise self.connect_error
        self.connections.append(
            {
                "profile": profile,
                "password": password,
                "timeout_ms": timeout_ms,
                "read_only": read_only,
            }
        )
        yield "CONN"

    def run_query(self, conn, sql, params, *, limit, max_bytes):
        assert conn == "CONN"
        self.queries.append({"sql": sql, "params": params, "limit": limit, "max_bytes": max_bytes})
        if self.query_error is not None:
            raise self.query_error
        if self.query_result_fn is not None:
            return dict(self.query_result_fn(len(self.queries)))
        return dict(self.query_result)

    def run_execute(self, conn, sql, params, *, dry_run, execute_verbs):
        assert conn == "CONN"
        self.executes.append(
            {"sql": sql, "params": params, "dry_run": dry_run, "execute_verbs": execute_verbs}
        )
        if self.execute_error is not None:
            raise self.execute_error
        return {
            "rows_affected": 3,
            "committed": not dry_run,
            "dry_run": dry_run,
            "elapsed_ms": 2,
        }

    def ping(self, profile, password, timeout_ms=None):
        self.pings.append({"profile": profile, "password": password})
        return {
            "ok": True,
            "dialect": profile["dialect"],
            "server_version": "16.4",
            "elapsed_ms": 1,
        }

    def sdb_get(self, uri):
        self.sdb.append(uri)
        return PG_PASSWORD


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    config = {"sqlx": CONFIG}
    salt = {
        "config.get": lambda key, default=None: config.get(key, default),
        "sdb.get": f.sdb_get,
    }
    monkeypatch.setattr(mod, "__salt__", salt, raising=False)
    monkeypatch.setattr(mod, "__opts__", {"id": "minion1"}, raising=False)
    monkeypatch.setattr(mod.engine, "connection", f.connection)
    monkeypatch.setattr(mod.engine, "ping", f.ping)
    monkeypatch.setattr(mod.runner, "run_query", f.run_query)
    monkeypatch.setattr(mod.runner, "run_execute", f.run_execute)
    f.config = config
    f.salt = salt
    return f


# --- __virtual__ ------------------------------------------------------------------------------


def test_virtual_ok():
    assert mod.__virtual__() == "sqlx"


def test_virtual_false_without_sqlalchemy(monkeypatch):
    """Loading the module with SQLAlchemy missing must give (False, reason), not an ImportError."""
    for name in [
        n
        for n in sys.modules
        if n == "sqlalchemy" or n.startswith(("sqlalchemy.", "saltext.sqlx.lib"))
    ]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    spec = importlib.util.spec_from_file_location("sqlx_mod_nosa", mod.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    ok, reason = fresh.__virtual__()
    assert ok is False
    assert "SQLAlchemy" in reason


def test_public_functions_are_exactly_spec_section_4():
    public = {
        n
        for n, v in vars(mod).items()
        if not n.startswith("_") and callable(v) and getattr(v, "__module__", None) == mod.__name__
    }
    assert public == {
        "query",
        "execute",
        "check_query_result",
        "check_row_count",
        "ping",
        "table_list",
    }


# --- query ------------------------------------------------------------------------------------


def test_query_returns_result_plus_profile(fake):
    out = mod.query(profile="pg_ro", sql=SQL, params={"x": 1})
    assert out == dict(ROWS, profile="pg_ro")
    json.dumps(out, allow_nan=False)
    assert fake.queries == [{"sql": SQL, "params": {"x": 1}, "limit": 500, "max_bytes": 1048576}]


def test_query_resolves_sdb_password_and_passes_it_to_the_engine(fake):
    mod.query(profile="pg_ro", sql="select 1")
    assert fake.sdb == ["sdb://vault/pg_ro"]
    assert fake.connections[0]["password"] == PG_PASSWORD
    assert fake.connections[0]["profile"]["dialect"] == "postgresql"


def test_query_literal_password_does_not_call_sdb(fake):
    mod.query(profile="pg_rw", sql="select 1")
    assert fake.sdb == []
    assert fake.connections[0]["password"] == RW_PASSWORD


def test_query_profile_without_password(fake):
    mod.query(profile="my_ro", sql="select 1")
    assert fake.connections[0]["password"] is None


def test_query_caps_can_only_lower_profile_limits(fake):
    mod.query(profile="pg_ro", sql="select 1", limit=10, timeout_ms=1000, max_bytes=2048)
    assert fake.queries[0]["limit"] == 10
    assert fake.queries[0]["max_bytes"] == 2048
    assert fake.connections[0]["timeout_ms"] == 1000
    mod.query(profile="pg_ro", sql="select 1", limit=10**9, timeout_ms=10**9, max_bytes=10**9)
    assert fake.queries[1]["limit"] == 500
    assert fake.queries[1]["max_bytes"] == 1048576
    assert fake.connections[1]["timeout_ms"] == 30000


def test_query_accepts_positional_arguments(fake):
    assert mod.query("pg_ro", "select 1")["profile"] == "pg_ro"


def test_pub_kwargs_are_ignored(fake):
    out = mod.query(
        profile="pg_ro",
        sql="select 1",
        __pub_fun="sqlx.query",
        __pub_jid="2026",
        __pub_user="root",
        __kwarg__=True,
    )
    assert out["profile"] == "pg_ro"


def test_unknown_option_is_e_option_unknown(fake):
    with pytest.raises(SaltInvocationError) as exc:
        mod.query(profile="pg_ro", sql="select 1", host="evil.example")
    assert str(exc.value).startswith("[E_OPTION_UNKNOWN]")
    assert fake.connections == []


@pytest.mark.parametrize("kwargs", [{}, {"profile": "pg_ro"}, {"sql": "select 1"}])
def test_missing_required_option(fake, kwargs):
    with pytest.raises(SaltInvocationError) as exc:
        mod.query(**kwargs)
    assert str(exc.value).startswith("[E_OPTION_INVALID]")
    assert fake.connections == []


def test_unknown_profile_is_e_profile_unknown(fake):
    with pytest.raises(SaltInvocationError) as exc:
        mod.query(profile="nope", sql="select 1")
    assert str(exc.value).startswith("[E_PROFILE_UNKNOWN]")
    assert fake.connections == [] and fake.sdb == []


def test_no_sqlx_config_is_e_profile_unknown(fake):
    fake.config.clear()
    with pytest.raises(SaltInvocationError) as exc:
        mod.query(profile="pg_ro", sql="select 1")
    assert str(exc.value).startswith("[E_PROFILE_UNKNOWN]")


def test_sdb_failure_is_e_secret_and_no_connection(fake):
    def boom(uri):
        raise RuntimeError("vault down " + PG_PASSWORD)

    fake.salt["sdb.get"] = boom
    with pytest.raises(CommandExecutionError) as exc:
        mod.query(profile="pg_ro", sql="select 1")
    assert str(exc.value).startswith("[E_SECRET]")
    assert PG_PASSWORD not in str(exc.value)
    assert fake.connections == []


# --- execute ----------------------------------------------------------------------------------


def test_execute_disabled_is_refused_before_any_database_or_secret_access(fake):
    with pytest.raises(CommandExecutionError) as exc:
        mod.execute(profile="pg_ro", sql="update t set a = 1")
    assert str(exc.value).startswith("[E_EXECUTE_DISABLED]")
    assert fake.connections == [] and fake.executes == [] and fake.sdb == [] and fake.pings == []


def test_execute_dry_run_false_by_default_and_commits(fake):
    out = mod.execute(profile="pg_rw", sql="update t set a = :a", params={"a": 1})
    assert out == {
        "profile": "pg_rw",
        "rows_affected": 3,
        "committed": True,
        "dry_run": False,
        "elapsed_ms": 2,
    }
    assert fake.executes[0]["dry_run"] is False
    assert fake.executes[0]["params"] == {"a": 1}
    assert fake.executes[0]["execute_verbs"] == ["INSERT", "UPDATE", "DELETE"]
    # an execute-enabled profile must be able to write: the read-only session guard is not applied
    assert fake.connections[0]["read_only"] is False
    json.dumps(out, allow_nan=False)


def test_execute_dry_run_true(fake):
    out = mod.execute(profile="pg_rw", sql="delete from t", dry_run=True, timeout_ms=500)
    assert out["dry_run"] is True and out["committed"] is False
    assert fake.connections[0]["timeout_ms"] == 500


def test_execute_policy_error_passes_through(fake):
    fake.execute_error = SaltInvocationError(
        "[E_POLICY] statement verb 'drop' is not in execute_verbs"
    )
    with pytest.raises(SaltInvocationError) as exc:
        mod.execute(profile="pg_rw", sql="drop table t")
    assert str(exc.value).startswith("[E_POLICY]")


# --- check_query_result / check_row_count -----------------------------------------------------


def test_check_query_result_true_and_false(fake):
    good = mod.check_query_result(
        profile="pg_ro", sql="select 1", operator="==", expected=12.5, row=0, column=1
    )
    assert good["ok"] is True and good["actual"] == "12.50" and good["profile"] == "pg_ro"
    bad = mod.check_query_result(
        profile="pg_ro", sql="select 1", operator=">", expected=100, row=0, column=1
    )
    assert bad["ok"] is False and bad["attempts"] == 1


def test_check_query_result_is_null_needs_no_expected(fake):
    out = mod.check_query_result(
        profile="pg_ro", sql="select 1", operator="is_null", row=1, column=1
    )
    assert out["ok"] is True and out["expected"] is None


def test_check_runs_with_profile_limits_and_no_call_limits(fake):
    mod.check_row_count(profile="pg_ro", sql="select 1", operator="==", expected=2)
    assert fake.queries[0]["limit"] == 500 and fake.queries[0]["max_bytes"] == 1048576
    assert fake.connections[0]["timeout_ms"] == 30000
    with pytest.raises(SaltInvocationError) as exc:
        mod.check_row_count(profile="pg_ro", sql="select 1", operator="==", expected=2, limit=5)
    assert str(exc.value).startswith("[E_OPTION_UNKNOWN]")


def test_check_row_count_ok_and_not_ok(fake):
    assert (
        mod.check_row_count(profile="pg_ro", sql="select 1", operator="==", expected=2)["ok"]
        is True
    )
    out = mod.check_row_count(profile="pg_ro", sql="select 1", operator=">", expected=5)
    assert out["ok"] is False and out["actual"] == 2 and out["profile"] == "pg_ro"


def test_check_row_count_truncated_is_unknown(fake):
    fake.query_result = dict(ROWS, truncated=True)
    out = mod.check_row_count(profile="pg_ro", sql="select 1", operator="==", expected=2)
    assert out["ok"] is None and out["reason"] == "truncated"


def test_check_retry_uses_a_new_connection_per_attempt(fake):
    fake.query_result_fn = lambda n: dict(ROWS, rows=[[n]], row_count=1)
    out = mod.check_query_result(
        profile="pg_ro",
        sql="select 1",
        operator="==",
        expected=3,
        retry_timeout_s=5,
        retry_pause_s=0.01,
    )
    assert out["ok"] is True and out["attempts"] == 3
    assert len(fake.connections) == 3


def test_check_bad_operator_is_e_option_invalid(fake):
    with pytest.raises(SaltInvocationError) as exc:
        mod.check_row_count(profile="pg_ro", sql="select 1", operator="contains", expected=1)
    assert str(exc.value).startswith("[E_OPTION_INVALID]")


def test_check_query_error_is_not_retried(fake):
    fake.query_error = db_error("42P01", 'relation "nope" does not exist')
    with pytest.raises(CommandExecutionError) as exc:
        mod.check_row_count(
            profile="pg_ro",
            sql=SQL,
            operator="==",
            expected=1,
            retry_timeout_s=5,
            retry_pause_s=0.01,
        )
    assert str(exc.value).startswith("[E_SQL] postgresql 42P01")
    assert len(fake.queries) == 1


# --- ping -------------------------------------------------------------------------------------


def test_ping(fake):
    out = mod.ping(profile="pg_ro")
    assert out == {
        "ok": True,
        "dialect": "postgresql",
        "server_version": "16.4",
        "elapsed_ms": 1,
        "profile": "pg_ro",
    }
    json.dumps(out)
    assert fake.pings[0]["password"] == PG_PASSWORD


def test_ping_failure_is_translated(fake, monkeypatch):
    def boom(profile, password, timeout_ms=None):
        raise db_error(
            "57014", "canceling statement due to statement timeout", sa.exc.OperationalError
        )

    monkeypatch.setattr(mod.engine, "ping", boom)
    with pytest.raises(CommandExecutionError) as exc:
        mod.ping(profile="pg_ro")
    assert (
        str(exc.value)
        == "[E_TIMEOUT] postgresql 57014: canceling statement due to statement timeout"
    )


# --- table_list -------------------------------------------------------------------------------


@pytest.fixture
def sqlite_conn(fake, monkeypatch):
    eng = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    with eng.begin() as conn:
        conn.execute(sa.text("create table orders (id integer)"))
        conn.execute(sa.text("create table items (id integer)"))
        conn.execute(sa.text("create view order_view as select * from orders"))

    @contextlib.contextmanager
    def connection(profile, password, timeout_ms=None, read_only=None):
        with eng.connect() as conn:
            yield conn

    monkeypatch.setattr(mod.engine, "connection", connection)
    yield
    eng.dispose()


def test_table_list_tables_only_by_default(fake, sqlite_conn):
    out = mod.table_list(profile="pg_ro")
    assert out == {"profile": "pg_ro", "schema": "main", "tables": ["items", "orders"], "views": []}
    json.dumps(out)


def test_table_list_with_views_schema_and_pattern(fake, sqlite_conn):
    out = mod.table_list(profile="pg_ro", schema="main", include_views=True)
    assert out["schema"] == "main"
    assert out["views"] == ["order_view"]
    out = mod.table_list(profile="pg_ro", pattern="ord%", include_views=True)
    assert out["tables"] == ["orders"] and out["views"] == ["order_view"]
    out = mod.table_list(profile="pg_ro", pattern="item_")
    assert out["tables"] == ["items"]
    assert mod.table_list(profile="pg_ro", pattern="o.ders")["tables"] == []


# --- errors -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (
            db_error("42601", 'syntax error at or near "selec"'),
            '[E_SQL] postgresql 42601: syntax error at or near "selec"',
        ),
        (
            db_error("42P01", 'relation "nope" does not exist'),
            '[E_SQL] postgresql 42P01: relation "nope" does not exist',
        ),
        (
            db_error("22012", "division by zero", sa.exc.DataError),
            "[E_SQL_DATA] postgresql 22012: division by zero",
        ),
        (
            db_error("23505", "duplicate key", sa.exc.IntegrityError),
            "[E_CONSTRAINT] postgresql 23505: duplicate key",
        ),
        (
            db_error("25006", "read-only transaction", sa.exc.InternalError),
            "[E_READ_ONLY] postgresql 25006: read-only transaction",
        ),
        (RuntimeError("anything " + PG_PASSWORD), "[E_INTERNAL] postgresql: RuntimeError"),
        (sa.exc.InvalidRequestError("A value is required for bind parameter 'x'"), None),
    ],
)
def test_query_failure_becomes_command_execution_error(fake, exc, expected):
    fake.query_error = exc
    with pytest.raises(CommandExecutionError) as caught:
        mod.query(profile="pg_ro", sql=SQL, params={"x": "bound-value-77"})
    message = str(caught.value)
    if expected:
        assert message == expected
    else:
        assert message.startswith("[E_OPTION_INVALID] postgresql")
    for forbidden in (
        "secret_column_name",
        "secret_table_name",
        "bound-value-77",
        PG_PASSWORD,
        "[SQL:",
    ):
        assert forbidden not in message
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_connect_failure_is_passed_through_translated(fake):
    fake.connect_error = CommandExecutionError(
        "[E_AUTH_FAILED] postgresql: password authentication failed"
    )
    with pytest.raises(CommandExecutionError) as exc:
        mod.query(profile="pg_ro", sql="select 1")
    assert str(exc.value) == "[E_AUTH_FAILED] postgresql: password authentication failed"


def test_error_detail_flag_comes_from_the_profile(fake):
    class Err(Exception):
        args = (1062, "Duplicate entry 'a@example.com' for key 'email'")
        sqlstate = "23000"

    fake.query_error = sa.exc.IntegrityError(SQL, {}, Err())
    with pytest.raises(CommandExecutionError) as exc:
        mod.query(profile="my_ro", sql=SQL)  # my_ro has error_detail: true
    assert "a@example.com" in str(exc.value)
    with pytest.raises(CommandExecutionError) as exc:
        mod.query(profile="pg_ro", sql=SQL)
    assert "a@example.com" not in str(exc.value)


# --- secrets: never in an exception, a log record or a return ---------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: mod.query(profile="pg_ro", sql=SQL),
        lambda: mod.execute(profile="pg_rw", sql="update t set a = 1"),
        lambda: mod.check_query_result(profile="pg_ro", sql=SQL, operator="==", expected=1),
        lambda: mod.check_row_count(profile="pg_ro", sql=SQL, operator="==", expected=1),
        lambda: mod.ping(profile="pg_ro"),
        lambda: mod.table_list(profile="pg_ro"),
    ],
    ids=["query", "execute", "check_query_result", "check_row_count", "ping", "table_list"],
)
@pytest.mark.parametrize("secret_profile", ["pg_ro", "pg_rw"])
def test_secret_is_never_in_exception_or_log(fake, monkeypatch, caplog, call, secret_profile):
    boom = RuntimeError(f"driver said password={PG_PASSWORD} and {RW_PASSWORD}")
    fake.query_error = boom
    fake.execute_error = boom
    monkeypatch.setattr(mod.engine, "ping", lambda *a, **k: (_ for _ in ()).throw(boom))

    caplog.set_level(logging.DEBUG)
    with pytest.raises(Exception) as exc:  # pylint: disable=broad-except
        call()
    # table_list has no fake runner: its connection fake yields a string, so inspect fails: still E_INTERNAL
    text = f"{exc.value!r} {exc.value} {exc.value.args}"
    chain = exc.value
    while chain is not None:
        text += repr(chain)
        chain = chain.__cause__ or chain.__context__
    text += caplog.text
    for forbidden in (PG_PASSWORD, RW_PASSWORD, "secret_column_name", "bound-value-77"):
        assert forbidden not in text
    assert str(exc.value).startswith("[E_")


def test_secret_not_in_successful_return_or_log(fake, caplog):
    caplog.set_level(logging.DEBUG)
    out = mod.query(profile="pg_ro", sql=SQL, params={"x": "bound-value-77"})
    assert PG_PASSWORD not in json.dumps(out)
    assert PG_PASSWORD not in caplog.text


def test_resolved_password_is_registered_with_the_masking_filter(fake, caplog):
    mod.query(profile="pg_ro", sql="select 1")
    caplog.set_level(logging.INFO)
    mod.log.info("connecting with %s and password=%s", PG_PASSWORD, "other")
    assert PG_PASSWORD not in caplog.text
    assert "other" not in caplog.text
    assert any(isinstance(f, mod.secrets.MaskingFilter) for f in mod.log.filters)


# --- audit ------------------------------------------------------------------------------------


def audit_lines(caplog):
    return [r for r in caplog.records if r.name == mod.log.name and r.levelno == logging.INFO]


def test_audit_line_for_success(fake, caplog):
    caplog.set_level(logging.INFO)
    mod.query(profile="pg_ro", sql=SQL, params={"x": "bound-value-77"}, __pub_user="alice")
    (record,) = audit_lines(caplog)
    line = record.getMessage()
    for part in (
        "function=query",
        "profile=pg_ro",
        "dialect=postgresql",
        f"sql_sha256={SQL_DIGEST}",
        "rows=2",
        "elapsed_ms=",
        "outcome=OK",
        "user=alice",
    ):
        assert part in line, part
    for forbidden in ("secret_column_name", "bound-value-77", PG_PASSWORD, "12.50"):
        assert forbidden not in line


def test_audit_line_for_failure_has_the_code(fake, caplog):
    caplog.set_level(logging.INFO)
    fake.query_error = db_error("42P01", "relation does not exist")
    with pytest.raises(CommandExecutionError):
        mod.query(profile="pg_ro", sql=SQL)
    (record,) = audit_lines(caplog)
    assert "outcome=E_SQL" in record.getMessage()
    assert "rows=-" in record.getMessage()
    assert "user=-" in record.getMessage()


def test_audit_line_for_execute_has_rows_affected(fake, caplog):
    caplog.set_level(logging.INFO)
    mod.execute(profile="pg_rw", sql="update t set a = 1")
    assert "rows=3" in audit_lines(caplog)[0].getMessage()


def test_audit_line_for_refused_execute_and_bad_option(fake, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(CommandExecutionError):
        mod.execute(profile="pg_ro", sql="update t set a = 1")
    with pytest.raises(SaltInvocationError):
        mod.query(profile="pg_ro\nforged line", sql=SQL, nope=1)
    first, second = audit_lines(caplog)
    assert "outcome=E_EXECUTE_DISABLED" in first.getMessage()
    assert "outcome=E_OPTION_UNKNOWN" in second.getMessage()
    assert "\n" not in second.getMessage() and "forged" not in second.getMessage()


def test_audit_for_ping_and_table_list_has_no_statement_hash(fake, caplog):
    caplog.set_level(logging.INFO)
    mod.ping(profile="pg_ro")
    assert "sql_sha256=-" in audit_lines(caplog)[0].getMessage()
