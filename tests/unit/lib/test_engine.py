"""Unit tests for saltext.sqlx.lib.engine (TEST_PLAN sections 3 and 4)."""

import types
from urllib.parse import quote
from urllib.parse import quote_plus

import pytest
import sqlalchemy
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from saltext.sqlx.lib import dialects
from saltext.sqlx.lib import engine

PASSWORD = "p@ss:w/o%rd?Zq9-secret"


def make(dialect="postgresql", **overrides):
    prof = {
        "name": "p",
        "dialect": dialect,
        "driver": dialects.DIALECT_DRIVERS[dialect],
        "host": "127.0.0.1",
        "port": 1,  # nothing listens here: real connection-refused errors, no network
        "database": "orders",
        "username": "salt_ro",
        "query": {},
        "connect_args": {},
        "read_only": True,
        "statement_timeout_ms": 30000,
        "connect_timeout_s": 3,
    }
    prof.update(overrides)
    return prof


@pytest.fixture(autouse=True)
def no_real_driver(monkeypatch):
    """
    Unit tests must not need psycopg/PyMySQL/oracledb/pymssql installed (the nox ``unit`` session installs none).
    Driver import is faked and ``create_engine`` returns a real NullPool engine on SQLite, recording the URL and
    keyword arguments that the module passed. Real-driver behaviour is covered by the functional tests.
    """
    created = []
    real_create = sqlalchemy.create_engine

    def fake_create(url, **kw):
        eng = real_create(
            "sqlite://",
            **{k: v for k, v in kw.items() if k in ("poolclass", "hide_parameters", "echo")},
        )
        created.append({"url": url, **kw})
        return eng

    monkeypatch.setattr(dialects.importlib, "import_module", types.ModuleType)
    monkeypatch.setattr(engine, "create_engine", fake_create)
    return created


def _all_text(exc):
    """Every string reachable from an exception: message, args, cause and context chain."""
    seen = []
    cur = exc
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return " | ".join(f"{type(e).__name__}: {e} {e.args!r}" for e in seen)


def _assert_clean(exc):
    text = _all_text(exc)
    for needle in (PASSWORD, "p%40ss", "w%2Fo", "salt_ro:", "://"):
        assert needle not in text, needle
    assert exc.__cause__ is None
    assert exc.__context__ is None


# --- build_engine -----------------------------------------------------------------------------


def test_build_engine_uses_nullpool(no_real_driver):
    eng = engine.build_engine(make(), PASSWORD)
    try:
        assert isinstance(eng, Engine)
        assert isinstance(eng.pool, NullPool)
    finally:
        eng.dispose()
    seen = no_real_driver[0]
    assert seen["poolclass"] is NullPool
    assert isinstance(seen["url"], sqlalchemy.engine.URL)
    assert seen["url"].password == PASSWORD
    assert seen["url"].drivername == "postgresql+psycopg"


def test_build_engine_hides_parameters_and_does_not_echo(no_real_driver):
    engine.build_engine(make(), PASSWORD).dispose()
    assert no_real_driver[0]["hide_parameters"] is True
    assert no_real_driver[0]["echo"] is False


def test_build_engine_passes_connect_timeout_and_allowlisted_args(no_real_driver):
    prof = make(connect_timeout_s=7, connect_args={"application_name": "salt"})
    engine.build_engine(prof, PASSWORD).dispose()
    assert no_real_driver[0]["connect_args"] == {
        "application_name": "salt",
        "connect_timeout": 7,
        "prepare_threshold": 0,
    }


def test_build_engine_rejects_forbidden_connect_args():
    with pytest.raises(SaltInvocationError) as exc:
        engine.build_engine(make(connect_args={"ssl_context": object()}), PASSWORD)
    assert str(exc.value).startswith("[E_POLICY]")


def test_build_engine_timeout_override_can_only_lower(no_real_driver):
    prof = make("mssql", statement_timeout_ms=10_000)
    engine.build_engine(prof, PASSWORD, timeout_ms=3000).dispose()
    assert no_real_driver[-1]["connect_args"]["timeout"] == 3
    engine.build_engine(prof, PASSWORD, timeout_ms=99_000).dispose()
    assert no_real_driver[-1]["connect_args"]["timeout"] == 10
    assert prof["statement_timeout_ms"] == 10_000


def test_build_engine_missing_driver(monkeypatch):
    def boom(name, *a, **k):
        raise ImportError("x")

    monkeypatch.setattr(dialects.importlib, "import_module", boom)
    with pytest.raises(CommandExecutionError) as exc:
        engine.build_engine(make(), PASSWORD)
    assert str(exc.value).startswith("[E_DRIVER_MISSING]")
    assert "saltext-sqlx[postgres]" in str(exc.value)


# --- managed_engine: always disposes ----------------------------------------------------------


class Spy:
    def __init__(self, monkeypatch):
        self.calls = 0
        real = Engine.dispose

        def dispose(this, *a, **k):
            self.calls += 1
            return real(this, *a, **k)

        monkeypatch.setattr(Engine, "dispose", dispose)


def test_managed_engine_disposes_on_success(monkeypatch):
    spy = Spy(monkeypatch)
    with engine.managed_engine(make(), PASSWORD) as eng:
        assert isinstance(eng.pool, NullPool)
    assert spy.calls == 1


def test_managed_engine_disposes_when_body_raises(monkeypatch):
    spy = Spy(monkeypatch)
    with pytest.raises(RuntimeError):
        with engine.managed_engine(make(), PASSWORD):
            raise RuntimeError("boom")
    assert spy.calls == 1


def _refuse(monkeypatch, message="connection failed: Connection refused"):
    def boom(self, *a, **k):
        raise sqlalchemy.exc.OperationalError("connect", {}, Exception(message))

    monkeypatch.setattr(Engine, "connect", boom)


def test_connection_disposes_when_connect_fails(monkeypatch):
    spy = Spy(monkeypatch)
    _refuse(monkeypatch)
    with pytest.raises(CommandExecutionError):
        with engine.connection(make(), PASSWORD):
            pass  # pragma: no cover
    assert spy.calls == 1


# --- first-connect failures -------------------------------------------------------------------


def test_connection_refused_is_e_connect_without_secrets(monkeypatch):
    _refuse(monkeypatch, "connection failed: Connection refused")
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(), PASSWORD):
            pass  # pragma: no cover
    assert str(exc.value) == "[E_CONNECT] postgresql: connection failed: Connection refused"
    _assert_clean(exc.value)


def test_oserror_at_connect_is_e_connect(monkeypatch):
    def boom(self, *a, **k):
        raise OSError(f"[Errno -2] Name or service not known {PASSWORD}")

    monkeypatch.setattr(Engine, "connect", boom)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(), PASSWORD):
            pass  # pragma: no cover
    # errors.raise_for is the one path: a raw OSError is not a driver error, so class name only
    assert str(exc.value) == "[E_INTERNAL] postgresql: OSError"
    _assert_clean(exc.value)


def test_sql_ascii_typeerror_gets_clear_utf8_message(monkeypatch):
    def boom(self, *a, **k):
        raise TypeError("cannot use a string pattern on a bytes-like object")

    monkeypatch.setattr(Engine, "connect", boom)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(), PASSWORD):
            pass  # pragma: no cover
    msg = str(exc.value)
    assert msg.startswith("[E_CONNECT] postgresql")
    assert "UTF8" in msg
    assert "SQL_ASCII" in msg
    assert "TypeError" not in msg
    _assert_clean(exc.value)


def test_other_typeerror_is_e_internal_class_name_only(monkeypatch):
    def boom(self, *a, **k):
        raise TypeError(f"bad thing with {PASSWORD}")

    monkeypatch.setattr(Engine, "connect", boom)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make("mysql"), PASSWORD):
            pass  # pragma: no cover
    assert str(exc.value) == "[E_INTERNAL] mysql: TypeError"
    _assert_clean(exc.value)


def test_bytes_typeerror_on_non_postgres_is_not_the_ascii_message(monkeypatch):
    def boom(self, *a, **k):
        raise TypeError("cannot use a string pattern on a bytes-like object")

    monkeypatch.setattr(Engine, "connect", boom)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make("mssql"), PASSWORD):
            pass  # pragma: no cover
    assert "SQL_ASCII" not in str(exc.value)


@pytest.mark.parametrize(
    "variant",
    [
        "Connection refused {pw}",
        "Connection refused user=salt_ro password={pw} host=h",
        "Connection refused postgresql://u:{q}@h/db",
        "Connection refused {q}",
        "Connection refused {qp}",
    ],
)
def test_driver_message_containing_password_is_scrubbed(monkeypatch, variant):
    text = variant.format(pw=PASSWORD, q=quote(PASSWORD, safe=""), qp=quote_plus(PASSWORD))

    def boom(self, *a, **k):
        raise sqlalchemy.exc.OperationalError("connect", {}, Exception(text))

    monkeypatch.setattr(Engine, "connect", boom)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(), PASSWORD):
            pass  # pragma: no cover
    assert str(exc.value).startswith("[E_CONNECT] postgresql")
    assert "***" in str(exc.value) or "<url>" in str(exc.value)
    _assert_clean(exc.value)


@pytest.mark.parametrize(
    "text,code,words",
    [
        (
            "connection failed: ... failed: fe_sendauth: no password supplied {pw}",
            "E_AUTH_FAILED",
            "no password supplied",
        ),
        ("connection timeout expired {pw}", "E_CONNECT", "connection timeout expired"),
        (
            "failed to resolve host 'h': [Errno -2] Name or service not known {pw}",
            "E_CONNECT",
            "Name or service not known",
        ),
        (
            'connection failed: ... FATAL:  no pg_hba.conf entry for host "h", user "u" {pw}',
            "E_CONNECT",
            "no pg_hba.conf entry for host",
        ),
    ],
)
def test_newly_mapped_connect_messages_are_real_and_scrubbed(monkeypatch, text, code, words):
    def boom(self, *a, **k):
        raise sqlalchemy.exc.OperationalError("connect", {}, Exception(text.format(pw=PASSWORD)))

    monkeypatch.setattr(Engine, "connect", boom)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(), PASSWORD):
            pass  # pragma: no cover
    assert str(exc.value).startswith(f"[{code}] postgresql: ")
    assert words in str(exc.value)  # the driver's own words survive
    _assert_clean(exc.value)


class _Orig(Exception):
    """Stand-in for a driver exception; attributes mimic the real drivers (SPEC section 8)."""


def _pg(sqlstate, message):
    err = _Orig(message)
    err.sqlstate = sqlstate
    return err


def _oracle(full_code, message):
    return _Orig(types.SimpleNamespace(full_code=full_code, message=message))


def _mssql(code, message):
    return _Orig((code, message.encode()))


@pytest.mark.parametrize(
    "dialect,err,code",
    [
        ("postgresql", _pg("28P01", "password authentication failed"), "E_AUTH_FAILED"),
        ("postgresql", _pg("28000", "no pg_hba.conf entry"), "E_AUTH_FAILED"),
        ("postgresql", _pg("08006", "connection failure"), "E_CONNECT"),
        ("postgresql", _Orig("connection failed: Connection refused"), "E_CONNECT"),
        # VERIFIED: psycopg 3 connect failures have no sqlstate; only the text identifies a bad password
        (
            "postgresql",
            _Orig('connection failed: FATAL:  password authentication failed for user "u"'),
            "E_AUTH_FAILED",
        ),
        ("mysql", _Orig(1045, "Access denied"), "E_AUTH_FAILED"),
        ("mysql", _Orig(2003, "Can't connect"), "E_CONNECT"),
        # Oracle and SQL Server: observed in T1b (docs/wire_shapes/t1b/), SPEC section 8.
        ("oracle", _oracle("DPY-6005", "cannot connect"), "E_CONNECT"),
        ("oracle", _oracle("ORA-01017", "invalid credential"), "E_AUTH_FAILED"),
        ("mssql", _mssql(20009, "Unable to connect"), "E_CONNECT"),
        ("mssql", _mssql(18456, "Login failed"), "E_AUTH_FAILED"),
    ],
)
def test_connect_failure_codes(monkeypatch, dialect, err, code):
    def boom(self, *a, **k):
        raise sqlalchemy.exc.OperationalError("connect", {}, err)

    monkeypatch.setattr(Engine, "connect", boom)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(dialect), PASSWORD):
            pass  # pragma: no cover
    assert str(exc.value).startswith(f"[{code}] {dialect}")
    _assert_clean(exc.value)


def test_error_message_is_single_line_and_bounded(monkeypatch):
    def boom(self, *a, **k):
        raise sqlalchemy.exc.OperationalError(
            "connect", {}, Exception("line1\nline2 " + "x" * 5000)
        )

    monkeypatch.setattr(Engine, "connect", boom)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(), PASSWORD):
            pass  # pragma: no cover
    assert "\n" not in str(exc.value)
    assert len(str(exc.value)) < 600


# --- connection: session setup ----------------------------------------------------------------


class ConnStub:
    def __init__(self):
        self.statements = []
        self.closed = False

    def exec_driver_sql(self, sql, *_):
        self.statements.append(sql)

    def close(self):
        self.closed = True


def test_connection_applies_session_setup_and_closes(monkeypatch):
    stub = ConnStub()
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: stub)
    with engine.connection(make(), PASSWORD, timeout_ms=2000) as conn:
        assert conn is stub
        assert stub.statements == ["SET statement_timeout = 2000", "SET TRANSACTION READ ONLY"]
    assert stub.closed


def test_connection_defaults_to_profile_timeout_and_read_only(monkeypatch):
    stub = ConnStub()
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: stub)
    with engine.connection(make(read_only=False), PASSWORD):
        pass
    assert stub.statements == ["SET statement_timeout = 30000"]


def test_connection_timeout_override_cannot_raise_profile_value(monkeypatch):
    stub = ConnStub()
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: stub)
    with engine.connection(make(statement_timeout_ms=5000), PASSWORD, timeout_ms=60_000):
        pass
    assert stub.statements[0] == "SET statement_timeout = 5000"


def test_connection_read_only_override(monkeypatch):
    stub = ConnStub()
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: stub)
    with engine.connection(make(), PASSWORD, read_only=False):
        pass
    assert stub.statements == ["SET statement_timeout = 30000"]


def test_connection_closes_and_disposes_when_body_raises(monkeypatch):
    stub = ConnStub()
    spy = Spy(monkeypatch)
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: stub)
    with pytest.raises(ValueError):
        with engine.connection(make(), PASSWORD):
            raise ValueError("x")
    assert stub.closed
    assert spy.calls == 1


def test_connection_setup_failure_closes_and_disposes(monkeypatch):
    class BadStub(ConnStub):
        def exec_driver_sql(self, sql, *_):
            raise RuntimeError("setup failed")

    stub = BadStub()
    spy = Spy(monkeypatch)
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: stub)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(), PASSWORD):
            pass  # pragma: no cover
    assert str(exc.value) == "[E_INTERNAL] postgresql: RuntimeError"
    assert stub.closed
    assert spy.calls == 1


# --- ping -------------------------------------------------------------------------------------


def test_ping_returns_spec_shape(monkeypatch):
    class Res:
        def scalar(self):
            return "16.4"

    class PingStub(ConnStub):
        def exec_driver_sql(self, sql, *_):
            self.statements.append(sql)
            return Res()

    stub = PingStub()
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: stub)
    out = engine.ping(make(), PASSWORD)
    assert out["ok"] is True
    assert out["dialect"] == "postgresql"
    assert out["server_version"] == "16.4"
    assert isinstance(out["elapsed_ms"], int)
    assert "SELECT 1" in stub.statements
    assert stub.closed


def test_ping_failure_propagates_e_connect(monkeypatch):
    _refuse(monkeypatch)
    with pytest.raises(CommandExecutionError) as exc:
        engine.ping(make(), PASSWORD)
    assert str(exc.value).startswith("[E_CONNECT]")
    _assert_clean(exc.value)


# --- one error path: failures after connect also go through errors.raise_for -------------------


def test_connection_uses_errors_raise_for_for_connect_failures(monkeypatch):
    seen = []
    real = engine.errors.raise_for

    def spy(exc, dialect, error_detail=False):
        seen.append((dialect, error_detail))
        return real(exc, dialect, error_detail=error_detail)

    monkeypatch.setattr(engine.errors, "raise_for", spy)
    _refuse(monkeypatch)
    with pytest.raises(CommandExecutionError):
        with engine.connection(make(error_detail=True), PASSWORD):
            pass  # pragma: no cover
    assert seen == [("postgresql", True)]


def test_session_setup_failure_goes_through_raise_for(monkeypatch):
    class BadStub(ConnStub):
        def exec_driver_sql(self, sql, *_):
            raise sqlalchemy.exc.ProgrammingError("SET", {}, _pg("42601", "syntax error"))

    stub = BadStub()
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: stub)
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(), PASSWORD):
            pass  # pragma: no cover
    assert str(exc.value) == "[E_SQL] postgresql 42601: syntax error"
    assert stub.closed
    _assert_clean(exc.value)


def test_ping_statement_failure_goes_through_raise_for(monkeypatch):
    class BadPing(ConnStub):
        def exec_driver_sql(self, sql, *_):
            if sql == "SELECT 1":
                raise sqlalchemy.exc.OperationalError(sql, {}, _pg("57014", "canceling statement"))

    stub = BadPing()
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: stub)
    with pytest.raises(CommandExecutionError) as exc:
        engine.ping(make(), PASSWORD)
    assert str(exc.value) == "[E_TIMEOUT] postgresql 57014: canceling statement"
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert stub.closed


def test_ping_raw_exception_is_e_internal_class_only(monkeypatch):
    class BadPing(ConnStub):
        def exec_driver_sql(self, sql, *_):
            if sql == "SELECT 1":
                raise RuntimeError(f"secret {PASSWORD}")

    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: BadPing())
    with pytest.raises(CommandExecutionError) as exc:
        engine.ping(make(), PASSWORD)
    assert str(exc.value) == "[E_INTERNAL] postgresql: RuntimeError"
    _assert_clean(exc.value)


def test_body_exceptions_propagate_unchanged(monkeypatch):
    monkeypatch.setattr(Engine, "connect", lambda self, *a, **k: ConnStub())
    with pytest.raises(CommandExecutionError) as exc:
        with engine.connection(make(), PASSWORD):
            raise CommandExecutionError("[E_EXECUTE_DISABLED] nope")
    assert str(exc.value) == "[E_EXECUTE_DISABLED] nope"
