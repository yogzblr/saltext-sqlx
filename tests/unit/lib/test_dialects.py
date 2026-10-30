"""Unit tests for saltext.sqlx.lib.dialects (TEST_PLAN section 3)."""

import ast
import pathlib
import re
import subprocess
import sys
import types

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError
from sqlalchemy.engine import URL
from sqlalchemy.engine import make_url

from saltext.sqlx.lib import dialects
from saltext.sqlx.lib import profiles

DIALECTS = ("postgresql", "mysql", "oracle", "mssql")

AWKWARD_PASSWORDS = [
    "p@ss",
    "a:b",
    "x/y",
    "100%",
    "q?z",
    "@:/%?",
    "p@ss:w/o%rd?x#y&=+ ",
    "%40%3A%2F",
    "pa'ss\"word",
]


def make(dialect="postgresql", **overrides):
    prof = {
        "name": "p",
        "dialect": dialect,
        "driver": dialects.DIALECT_DRIVERS[dialect],
        "host": "db1.internal",
        "port": 5432,
        "database": "orders",
        "username": "salt_ro",
        "query": {},
        "connect_args": {},
        "read_only": True,
        "statement_timeout_ms": 30000,
        "connect_timeout_s": 10,
    }
    prof.update(overrides)
    return prof


def _policy(func, *args):
    with pytest.raises(SaltInvocationError) as exc:
        func(*args)
    assert str(exc.value).startswith("[E_POLICY]"), str(exc.value)
    return str(exc.value)


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class FakeConn:
    """Records exec_driver_sql calls; mimics the bits of Connection the adapters touch."""

    def __init__(self, scalar=None, is_mariadb=False, driver_connection=None):
        self.statements = []
        self._scalar = scalar
        self.dialect = types.SimpleNamespace(is_mariadb=is_mariadb)
        self.connection = types.SimpleNamespace(
            driver_connection=driver_connection or types.SimpleNamespace()
        )

    def exec_driver_sql(self, sql, *args):
        assert not args, "module statements must be constant strings"
        self.statements.append(sql)
        return FakeResult(self._scalar)


# --- single source of truth -------------------------------------------------------------------


def test_dialect_drivers_single_source():
    assert dialects.DIALECT_DRIVERS == {
        "postgresql": "psycopg",
        "mysql": "pymysql",
        "oracle": "oracledb",
        "mssql": "pymssql",
    }
    assert profiles.DIALECT_DRIVERS is dialects.DIALECT_DRIVERS


@pytest.mark.parametrize("dialect", DIALECTS)
def test_adapter_table(dialect):
    adapter = dialects.adapter(dialect)
    assert adapter.name == dialect
    assert adapter.driver == dialects.DIALECT_DRIVERS[dialect]
    assert adapter.pip_extra in {"postgres", "mysql", "oracle", "mssql"}
    assert adapter.ping_sql.upper().startswith("SELECT 1")


def test_adapter_unknown_dialect():
    with pytest.raises(SaltInvocationError) as exc:
        dialects.adapter("sqlite")
    assert str(exc.value).startswith("[E_PROFILE_INVALID]")


def test_ping_sql_values():
    assert dialects.adapter("oracle").ping_sql == "SELECT 1 FROM DUAL"
    assert dialects.adapter("postgresql").ping_sql == "SELECT 1"


def test_mariadb_is_not_a_dialect():
    """SPEC section 3 lists four dialects; MariaDB is the mysql adapter with a server-side variant."""
    assert "mariadb" not in dialects.DIALECT_DRIVERS


def test_drivers_are_imported_lazily():
    code = (
        "import sys\n"
        "import saltext.sqlx.lib.dialects as d\n"
        "d.adapter('postgresql')\n"
        "bad = [m for m in ('psycopg', 'pymysql', 'oracledb', 'pymssql') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
        cwd="/",
    )
    assert proc.returncode == 0, proc.stderr


def test_no_toplevel_driver_import_in_dialects_source():
    src = pathlib.Path(dialects.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    drivers = set(dialects.DIALECT_DRIVERS.values())
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            continue
        assert not names & drivers


# --- driver loading ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_load_driver_missing_names_only_pip_extra(dialect, monkeypatch):
    def boom(name, *a, **k):
        raise ImportError(f"No module named {name!r} at /secret/path/site-packages")

    monkeypatch.setattr(dialects.importlib, "import_module", boom)
    adapter = dialects.adapter(dialect)
    with pytest.raises(CommandExecutionError) as exc:
        adapter.load_driver()
    msg = str(exc.value)
    assert msg.startswith("[E_DRIVER_MISSING]")
    assert f"saltext-sqlx[{adapter.pip_extra}]" in msg
    assert "/secret/path" not in msg
    assert "No module named" not in msg
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_load_driver_returns_module(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(dialects.importlib, "import_module", lambda name: sentinel)
    assert dialects.adapter("mssql").load_driver() is sentinel


def test_missing_driver_only_affects_its_dialect(monkeypatch):
    def selective(name):
        if name == "oracledb":
            raise ImportError("nope")
        return types.ModuleType(name)

    monkeypatch.setattr(dialects.importlib, "import_module", selective)
    with pytest.raises(CommandExecutionError):
        dialects.adapter("oracle").load_driver()
    assert (
        dialects.adapter("postgresql").load_driver().__name__ == "psycopg"
    )  # fake module, no real driver needed


# --- URL --------------------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("password", AWKWARD_PASSWORDS)
def test_url_awkward_passwords_roundtrip(dialect, password):
    url = dialects.adapter(dialect).url(make(dialect), password)
    assert isinstance(url, URL)
    assert url.password == password
    rendered = url.render_as_string(hide_password=False)
    assert make_url(rendered).password == password
    assert make_url(rendered).username == "salt_ro"


@pytest.mark.parametrize(
    "dialect,drivername",
    [
        ("postgresql", "postgresql+psycopg"),
        ("mysql", "mysql+pymysql"),
        ("oracle", "oracle+oracledb"),
        ("mssql", "mssql+pymssql"),
    ],
)
def test_url_drivername_and_parts(dialect, drivername):
    url = dialects.adapter(dialect).url(make(dialect, port=1234), "pw")
    assert url.drivername == drivername
    assert (url.host, url.port, url.username) == ("db1.internal", 1234, "salt_ro")


def test_url_database_for_non_oracle():
    for dialect in ("postgresql", "mysql", "mssql"):
        assert dialects.adapter(dialect).url(make(dialect), "pw").database == "orders"


def test_url_oracle_database_is_service_name():
    """UNVERIFIED(T1): SQLAlchemy reads URL.database as a SID; the profile database is a service name."""
    url = dialects.adapter("oracle").url(make("oracle"), "pw")
    assert url.database is None
    assert url.query["service_name"] == "orders"


def test_url_without_password():
    url = dialects.adapter("postgresql").url(make(), None)
    assert url.password is None


def test_url_never_built_by_string_formatting():
    src = pathlib.Path(dialects.__file__).read_text(encoding="utf-8")
    assert "URL.create(" in src
    assert "://" not in src.replace("drivername", "")


def test_url_query_passthrough():
    url = dialects.adapter("postgresql").url(
        make(query={"sslmode": "verify-full", "application_name": "salt"}), "pw"
    )
    assert dict(url.query) == {"sslmode": "verify-full", "application_name": "salt"}


def test_url_query_bool_and_int_become_strings():
    url = dialects.adapter("postgresql").url(make(query={"keepalives": 1}), "pw")
    assert url.query["keepalives"] == "1"
    url = dialects.adapter("mysql").url(make("mysql", query={"ssl_disabled": True}), "pw")
    assert url.query["ssl_disabled"] == "true"


@pytest.mark.parametrize(
    "dialect,key",
    [
        ("mysql", "local_infile"),
        ("mysql", "client_flag"),
        ("mysql", "init_command"),
        ("postgresql", "options"),
        ("postgresql", "sslkey"),
        ("oracle", "mode"),
        ("mssql", "conn_properties"),
        ("postgresql", "totally_unknown"),
    ],
)
def test_url_query_rejects_non_allowlisted_keys(dialect, key):
    """URL query args reach the driver too, so they get the same allowlist as connect_args."""
    msg = _policy(dialects.adapter(dialect).url, make(dialect, query={key: "x"}), "pw")
    assert key in msg


def test_url_query_service_name_reserved_for_oracle_database():
    _policy(dialects.adapter("oracle").url, make("oracle", query={"service_name": "x"}), "pw")


# --- connect_args allowlist -------------------------------------------------------------------


@pytest.mark.parametrize(
    "dialect,key,value",
    [
        ("postgresql", "application_name", "salt"),
        ("postgresql", "sslmode", "verify-full"),
        ("postgresql", "keepalives_idle", 30),
        ("mysql", "charset", "utf8mb4"),
        ("mysql", "read_timeout", 5),
        ("oracle", "expire_time", 1),
        ("mssql", "appname", "salt"),
        ("mssql", "tds_version", "7.4"),
    ],
)
def test_connect_args_allowed(dialect, key, value):
    args = dialects.adapter(dialect).connect_args(make(dialect, connect_args={key: value}))
    assert args[key] == value


@pytest.mark.parametrize(
    "dialect,key",
    [
        ("postgresql", "ssl_context"),
        ("postgresql", "options"),
        ("postgresql", "password"),
        ("postgresql", "host"),
        ("postgresql", "sslkey"),
        ("postgresql", "connect_timeout"),
        ("mysql", "local_infile"),
        ("mysql", "client_flag"),
        ("mysql", "init_command"),
        ("mysql", "ssl"),
        ("mysql", "read_default_file"),
        ("mysql", "connect_timeout"),
        ("mysql", "autocommit"),
        ("oracle", "mode"),
        ("oracle", "ssl_context"),
        ("oracle", "tcp_connect_timeout"),
        ("oracle", "wallet_location"),
        ("mssql", "conn_properties"),
        ("mssql", "login_timeout"),
        ("mssql", "timeout"),
        ("mssql", "allowLoadLocalInfile"),
    ],
)
def test_connect_args_rejected(dialect, key):
    msg = _policy(dialects.adapter(dialect).connect_args, make(dialect, connect_args={key: 1}))
    assert key in msg


@pytest.mark.parametrize("value", [lambda: 1, print, object(), [1], {"a": 1}, b"x", None])
def test_connect_args_rejects_non_scalar_values(value):
    prof = make(connect_args={"application_name": value})
    msg = _policy(dialects.adapter("postgresql").connect_args, prof)
    assert "application_name" in msg


def test_connect_args_error_never_echoes_value():
    secret = "hunter2-value"
    msg = _policy(
        dialects.adapter("postgresql").connect_args,
        make(connect_args={"ssl_context": secret}),
    )
    assert secret not in msg


@pytest.mark.parametrize(
    "dialect,expected",
    [
        ("postgresql", {"connect_timeout": 7}),
        ("mysql", {"connect_timeout": 7}),
        ("oracle", {"tcp_connect_timeout": 7}),
        ("mssql", {"login_timeout": 7, "timeout": 30}),
    ],
)
def test_connect_args_include_connect_timeout_from_profile(dialect, expected):
    args = dialects.adapter(dialect).connect_args(make(dialect, connect_timeout_s=7))
    for key, value in expected.items():
        assert args[key] == value


def test_connect_args_mssql_timeout_rounds_up_seconds():
    args = dialects.adapter("mssql").connect_args(make("mssql", statement_timeout_ms=1500))
    assert args["timeout"] == 2


def test_connect_args_do_not_mutate_profile():
    prof = make(connect_args={"application_name": "x"})
    dialects.adapter("postgresql").connect_args(prof)
    assert prof["connect_args"] == {"application_name": "x"}


# --- session_setup ----------------------------------------------------------------------------


def test_pg_session_setup_sql():
    conn = FakeConn()
    dialects.adapter("postgresql").session_setup(conn, make(), 5000, True)
    assert conn.statements == ["SET statement_timeout = 5000", "SET TRANSACTION READ ONLY"]


def test_pg_session_setup_not_read_only():
    conn = FakeConn()
    dialects.adapter("postgresql").session_setup(conn, make(), 5000, False)
    assert conn.statements == ["SET statement_timeout = 5000"]


def test_mysql_session_setup_sql():
    conn = FakeConn()
    dialects.adapter("mysql").session_setup(conn, make("mysql"), 5000, True)
    assert conn.statements == [
        "SET SESSION MAX_EXECUTION_TIME = 5000",
        "SET SESSION TRANSACTION READ ONLY",
    ]


def test_mariadb_session_setup_uses_seconds():
    conn = FakeConn(is_mariadb=True)
    dialects.adapter("mysql").session_setup(conn, make("mysql"), 2500, False)
    assert conn.statements == ["SET SESSION max_statement_time = 2.500"]


def test_oracle_session_setup_sets_call_timeout_and_read_only():
    drv = types.SimpleNamespace()
    conn = FakeConn(driver_connection=drv)
    dialects.adapter("oracle").session_setup(conn, make("oracle"), 4000, True)
    assert drv.call_timeout == 4000
    assert conn.statements == ["SET TRANSACTION READ ONLY"]


def test_mssql_session_setup_runs_nothing():
    conn = FakeConn()
    dialects.adapter("mssql").session_setup(conn, make("mssql"), 4000, True)
    assert not conn.statements


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("read_only", [True, False])
def test_session_sql_is_constant_text_plus_one_integer(dialect, read_only):
    conn = FakeConn(driver_connection=types.SimpleNamespace())
    dialects.adapter(dialect).session_setup(conn, make(dialect), "5000", read_only)
    for sql in conn.statements:
        assert re.fullmatch(r"SET [A-Za-z_ ]+?( = \d+(\.\d{3})?)?", sql), sql


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "bad",
    [
        "5000; DROP TABLE x",
        "1 OR 1=1",
        "0x10",
        "",
        None,
        True,
        1.5,
        [5000],
        -1,
        0,
    ],
)
def test_session_setup_rejects_non_integer_timeout(dialect, bad):
    conn = FakeConn()
    with pytest.raises(SaltInvocationError) as exc:
        dialects.adapter(dialect).session_setup(conn, make(dialect), bad, True)
    assert str(exc.value).startswith("[E_OPTION_INVALID]")
    assert not conn.statements


@pytest.mark.parametrize("dialect", DIALECTS)
def test_session_setup_rejects_timeout_above_hard_cap(dialect):
    conn = FakeConn()
    _policy(dialects.adapter(dialect).session_setup, conn, make(dialect), 300_001, True)
    assert not conn.statements


@pytest.mark.parametrize("dialect", DIALECTS)
def test_session_setup_rejects_non_bool_read_only(dialect):
    conn = FakeConn()
    with pytest.raises(SaltInvocationError) as exc:
        dialects.adapter(dialect).session_setup(conn, make(dialect), 1000, "yes")
    assert str(exc.value).startswith("[E_OPTION_INVALID]")


# --- version ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dialect,sql",
    [
        ("postgresql", "SHOW server_version"),
        ("mysql", "SELECT VERSION()"),
        ("mssql", "SELECT @@VERSION"),
    ],
)
def test_version_runs_constant_sql(dialect, sql):
    conn = FakeConn(scalar="16.4")
    assert dialects.adapter(dialect).version(conn) == "16.4"
    assert conn.statements == [sql]


def test_version_oracle_uses_driver_attribute():
    conn = FakeConn(driver_connection=types.SimpleNamespace(version="23.4.0.24"))
    assert dialects.adapter("oracle").version(conn) == "23.4.0.24"
    assert not conn.statements


def test_version_is_always_text():
    assert dialects.adapter("mysql").version(FakeConn(scalar=8)) == "8"
    assert dialects.adapter("mysql").version(FakeConn(scalar=None)) == ""


# --- T11 D1: PostgreSQL must reject multi-statement strings -------------------------------------
def test_postgresql_connect_args_force_the_extended_protocol():
    """``prepare_threshold=0`` is what makes PostgreSQL refuse ``a; b`` (T11 D1); it is module-owned."""
    assert dialects.adapter("postgresql").connect_args(make())["prepare_threshold"] == 0


def test_prepare_threshold_cannot_be_set_from_a_profile():
    prof = make(connect_args={"prepare_threshold": 5})
    _policy(dialects.adapter("postgresql").connect_args, prof)


# --- T11 D2: PyMySQL read_timeout is the client-side backstop ------------------------------------
def test_mysql_read_timeout_is_statement_timeout_plus_margin():
    args = dialects.adapter("mysql").connect_args(make("mysql", statement_timeout_ms=800))
    assert args["read_timeout"] == pytest.approx(1.8)


def test_mysql_read_timeout_follows_the_effective_statement_timeout():
    args = dialects.adapter("mysql").connect_args(make("mysql", statement_timeout_ms=30000))
    assert args["read_timeout"] == pytest.approx(31.0)


@pytest.mark.parametrize("where", ["connect_args", "query"])
def test_mysql_profile_may_lower_but_not_raise_read_timeout(where):
    lower = dialects.adapter("mysql").connect_args(make("mysql", **{where: {"read_timeout": 5}}))
    assert lower["read_timeout"] == 5
    higher = dialects.adapter("mysql").connect_args(make("mysql", **{where: {"read_timeout": 999}}))
    assert higher["read_timeout"] == pytest.approx(31.0)


@pytest.mark.parametrize("dialect", ["postgresql", "oracle", "mssql"])
def test_only_the_mysql_family_gets_read_timeout(dialect):
    assert "read_timeout" not in dialects.adapter(dialect).connect_args(make(dialect))


def test_postgresql_withheld_password_blocks_the_libpq_environment_and_passfile():
    args = dialects.adapter("postgresql").connect_args(make("postgresql", password_dropped=True))
    assert args["password"] == "" and args["passfile"].startswith("/nonexistent/")
    plain = dialects.adapter("postgresql").connect_args(make("postgresql"))
    assert "password" not in plain and "passfile" not in plain
