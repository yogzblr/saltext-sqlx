"""Functional tests for call-time connection parameters against REAL servers (SPEC section 3.2).

PostgreSQL 54329, MySQL 33061, MariaDB 33062 (``tests-support/env.sh``). The base profiles deliberately point at a
closed port with a wrong account, so a call only works if the call-supplied host/port/user/password/database are
really the ones used. ``__salt__`` is injected as in the other functional module tests. A disallowed host must never
receive a TCP connection: a listener the test owns counts connection attempts. Scratch tables are ``scratch.t12_*``.
"""

# pylint: disable=redefined-outer-name,unused-argument
import contextlib
import socket
import threading
import time
import uuid

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.functional.test_runner_functional import DIALECTS
from tests.functional.test_runner_functional import HOST
from tests.functional.test_runner_functional import _reachable

pytestmark = [pytest.mark.pg, pytest.mark.mysql, pytest.mark.mariadb]

SDB = {"sdb://vault/ro": "ro_pw", "sdb://vault/rw": "rw_pw", "sdb://vault/bad": "not-the-password"}
ALLOW = [
    "dialect",
    "host",
    "port",
    "database",
    "username",
    "password",
    "statement_timeout_ms",
    "read_only",
]


@contextlib.contextmanager
def listener():
    """A TCP listener on loopback that counts connection attempts (``accepted``) and hangs up at once.

    Accepting in a thread matters: PyMySQL waits without a timeout for the server greeting, so a listener that only
    accepts when the test asks would hang the client.
    """
    sock = socket.socket()
    sock.bind((HOST, 0))
    sock.listen(5)
    seen = []

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            seen.append(1)
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    class L:
        port = sock.getsockname()[1]

        @staticmethod
        def accepted():
            time.sleep(0.2)  # let the accept thread run
            return len(seen)

    try:
        yield L
    finally:
        sock.close()
        thread.join(timeout=2)


@pytest.fixture(scope="module", params=DIALECTS)
def server(request):
    srv, reason = _reachable(request.param)
    if reason:
        pytest.skip(reason)
    srv.table = f"scratch.t12_{uuid.uuid4().hex[:10]}"
    srv.admin_run(f"CREATE TABLE {srv.table} (k varchar(20) PRIMARY KEY, n integer)")
    if srv.key == "postgresql":
        srv.admin_run(f"GRANT SELECT ON {srv.table} TO ro")
        srv.admin_run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {srv.table} TO rw")
    try:
        yield srv
    finally:
        srv.admin_run(f"DROP TABLE IF EXISTS {srv.table}")


@pytest.fixture
def cfg(server, monkeypatch):
    """Profiles ``base`` / ``base_rw`` that cannot connect on their own (closed port, wrong account)."""
    dead = {
        "dialect": server.dialect,
        "host": HOST,
        "port": 1,
        "database": server.cfg["database"],
        "username": "nobody",
        "password": "nobody_pw",
        "connect_timeout_s": 5,
    }
    config = {
        "profiles": {
            "base": dict(dead),
            "base_rw": dict(dead, allow_execute=True, read_only=False),
        },
        "_policy": {
            "allow_call_params": list(ALLOW),
            "allowed_hosts": ["127.0.0.1"],
            "allow_adhoc_connection": True,
        },
        "default_profile": "base",
    }
    monkeypatch.setattr(
        mod,
        "__salt__",
        {
            "config.get": lambda key, default=None: config if key == "sqlx" else default,
            "sdb.get": SDB.__getitem__,
        },
        raising=False,
    )
    server.admin_run(f"DELETE FROM {server.table}")
    server.admin_run(f"INSERT INTO {server.table} (k, n) VALUES ('a', 1), ('b', 2)")
    config["server"] = server
    return config


def target(server, **extra):
    base = {
        "host": HOST,
        "port": server.cfg["port"],
        "username": "ro",
        "password": "sdb://vault/ro",
    }
    base.update(extra)
    return base


def who(server):
    return "select current_user" if server.key == "postgresql" else "select current_user()"


# -- the call-supplied parameters are the ones used ---------------------------------------------------------------------


def test_the_base_profile_alone_cannot_connect(cfg, server):
    with pytest.raises(CommandExecutionError, match=r"^\[E_CONNECT\]"):
        mod.ping(profile="base")


@pytest.mark.parametrize("style", ["flat", "connection"])
def test_call_supplied_host_port_user_password_are_used(cfg, server, style):
    kw = target(server)
    kw = kw if style == "flat" else {"connection": kw}
    out = mod.query(profile="base", sql=who(server), **kw)
    assert out["rows"][0][0].split("@")[0] == "ro"
    assert out["profile"] == "base"


def test_call_supplied_database(cfg, server):
    sql = "select current_database()" if server.key == "postgresql" else "select database()"
    out = mod.query(profile="base", sql=sql, database=server.cfg["database"], **target(server))
    assert out["rows"][0][0] == server.cfg["database"]


def test_default_profile_is_the_base(cfg, server):
    assert mod.query(sql=who(server), **target(server))["profile"] == "base"


def test_adhoc_connection_without_any_profile(cfg, server):
    del cfg["default_profile"]
    adhoc = target(server, dialect=server.dialect, database=server.cfg["database"])
    out = mod.query(sql=who(server), **adhoc)
    assert out["profile"] == "(adhoc)" and out["rows"][0][0].split("@")[0] == "ro"
    assert mod.ping(**adhoc)["dialect"] == server.dialect


def test_wrong_call_password_is_the_real_auth_error_without_the_reference(cfg, server):
    with pytest.raises(CommandExecutionError) as exc:
        mod.ping(profile="base", **target(server, password="sdb://vault/bad"))
    assert str(exc.value).startswith("[E_AUTH_FAILED]")
    assert "vault/bad" not in str(exc.value) and "not-the-password" not in str(exc.value)


def test_unresolvable_call_reference_is_e_secret_without_the_reference(cfg, server):
    with pytest.raises(CommandExecutionError) as exc:
        mod.ping(profile="base", **target(server, password="sdb://vault/NOPE_REF"))
    assert str(exc.value).startswith("[E_SECRET]") and "NOPE_REF" not in str(exc.value)


def test_every_connecting_function_takes_the_parameters(cfg, server):
    kw = target(server)
    sql = f"select count(*) from {server.table}"
    assert mod.check_query_result(profile="base", sql=sql, operator="==", expected=2, **kw)["ok"]
    assert mod.check_row_count(
        profile="base", sql=f"select k from {server.table}", operator="==", expected=2, **kw
    )["ok"]
    assert mod.ping(profile="base", **kw)["ok"]
    tables = mod.table_list(
        profile="base", pattern=server.table.split(".")[1], schema="scratch", **kw
    )
    assert tables["tables"] == [server.table.split(".")[1]]


def test_execute_with_call_parameters_commits(cfg, server):
    out = mod.execute(
        profile="base_rw",
        sql=f"insert into {server.table} (k, n) values ('z', 9)",
        **target(server, username="rw", password="sdb://vault/rw"),
    )
    assert out["rows_affected"] == 1 and out["committed"] is True
    assert server.verify(f"select n from {server.table} where k = 'z'") == [(9,)]


def test_execute_still_needs_allow_execute_on_the_profile(cfg, server):
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.execute(
            profile="base",
            sql=f"insert into {server.table} values ('y', 1)",
            **target(server, username="rw", password="sdb://vault/rw"),
        )
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        del cfg["default_profile"]
        mod.execute(
            sql=f"insert into {server.table} values ('y', 1)",
            **target(
                server,
                username="rw",
                password="sdb://vault/rw",
                dialect=server.dialect,
                database=server.cfg["database"],
            ),
        )
    assert server.verify(f"select count(*) from {server.table} where k = 'y'") == [(0,)]


def test_a_call_cannot_set_allow_execute(cfg, server):
    for kw in ({"allow_execute": True}, {"connection": {"allow_execute": True}}):
        with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
            mod.execute(profile="base", sql="insert into x values (1)", **kw, **target(server))


# -- read-only and caps -----------------------------------------------------------------------------------------------


def test_read_only_can_be_turned_on_by_a_call_and_blocks_a_write(cfg, server):
    insert = f"insert into {server.table} (k, n) values ('q', 1)"
    rw = target(server, username="rw", password="sdb://vault/rw")
    # control: the rw query profile has read_only false, so the statement is accepted (the transaction is rolled back)
    cfg["profiles"]["base"]["read_only"] = False
    assert mod.query(profile="base", sql=insert, **rw)["row_count"] == 0
    with pytest.raises(CommandExecutionError, match=r"^\[E_READ_ONLY\]"):
        mod.query(profile="base", sql=insert, read_only=True, **rw)
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*read_only"):
        mod.query(profile="base", sql="select 1", read_only=False, **rw)


@pytest.mark.pg
def test_statement_timeout_can_be_lowered(cfg, server):
    if server.key != "postgresql":
        pytest.skip("pg_sleep statement timeout is verified on PostgreSQL only here")
    with pytest.raises(CommandExecutionError, match=r"^\[E_TIMEOUT\]"):
        mod.query(
            profile="base", sql="select pg_sleep(3)", statement_timeout_ms=200, **target(server)
        )
    # a call cannot raise the ceiling: 10**9 is clamped to the default 30 s, so a 1 s sleep succeeds
    assert (
        mod.query(
            profile="base", sql="select pg_sleep(1)", statement_timeout_ms=10**9, **target(server)
        )["row_count"]
        == 1
    )


# -- a disallowed host never gets a TCP connection -----------------------------------------------------------------------


@pytest.mark.parametrize("style", ["flat", "connection"])
def test_a_host_outside_allowed_hosts_is_refused_before_any_tcp_connection(cfg, server, style):
    cfg["_policy"]["allowed_hosts"] = ["10.0.0.0/8", "*.corp.example"]
    with listener() as spy:
        kw = target(server, port=spy.port)
        with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]") as exc:
            mod.ping(profile="base", **(kw if style == "flat" else {"connection": kw}))
        assert spy.accepted() == 0
    assert "127.0.0.1" not in str(exc.value) and "ro_pw" not in str(exc.value)


def test_loopback_is_refused_without_an_explicit_ip_entry(cfg, server):
    cfg["_policy"]["allowed_hosts"] = ["127.*", "localhost"]
    with listener() as spy:
        for host in ("127.0.0.1", "localhost"):
            with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
                mod.ping(profile="base", **target(server, host=host, port=spy.port))
        assert spy.accepted() == 0


def test_metadata_address_never_gets_a_connection(cfg, server):
    cfg["_policy"]["allowed_hosts"] = ["169.254.*", "10.0.0.0/8"]
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.ping(profile="base", **target(server, host="169.254.169.254"))


def test_the_listener_does_receive_a_connection_when_the_policy_allows_it(cfg, server):
    """Control for the tests above: the counter works. With the loopback IP allowed the attempt is real."""
    with listener() as spy:
        with pytest.raises(CommandExecutionError):  # the listener is not a database
            mod.ping(profile="base", **target(server, port=spy.port))
        assert spy.accepted() >= 1


def test_field_not_in_allow_call_params_is_refused_without_connecting(cfg, server):
    cfg["_policy"]["allow_call_params"] = ["database"]
    with listener() as spy:
        with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
            mod.ping(profile="base", **target(server, port=spy.port))
        assert spy.accepted() == 0


# -- psql_query --------------------------------------------------------------------------------------------------------


@pytest.mark.pg
def test_psql_query_on_postgresql(cfg, server):
    if server.key != "postgresql":
        with pytest.raises(SaltInvocationError, match=r"postgresql profile"):
            mod.psql_query("select 1", profile="base")
        return
    out = mod.psql_query(
        "select 1 as one, null as nothing, 'x' as s;",
        user="ro",
        host=HOST,
        port=server.cfg["port"],
        maintenance_db=server.cfg["database"],
        password="sdb://vault/ro",
    )
    assert out == [{"one": "1", "nothing": None, "s": "x"}]
    with pytest.raises(
        CommandExecutionError,
        match=r'^\[E_SQL\] postgresql 42P01: relation "t12_nope" does not exist',
    ):
        mod.psql_query(
            "select * from t12_nope",
            **{
                "user": "ro",
                "host": HOST,
                "port": server.cfg["port"],
                "password": "sdb://vault/ro",
            },
        )
    with pytest.raises(SaltInvocationError, match="runas is not supported"):
        mod.psql_query("select 1", runas="postgres")


@pytest.mark.pg
def test_psql_query_write_goes_through_execute(cfg, server):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    conn = {"user": "rw", "host": HOST, "port": server.cfg["port"], "password": "sdb://vault/rw"}
    out = mod.psql_query(
        f"insert into {server.table} (k, n) values ('w', 5)", write=True, profile="base_rw", **conn
    )
    assert out == [{"rows_affected": "1"}]
    assert server.verify(f"select n from {server.table} where k = 'w'") == [(5,)]
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.psql_query("update x set a = 1", write=True, profile="base", **conn)
    with pytest.raises(
        SaltInvocationError, match=r"^\[E_POLICY\]"
    ):  # the verb filter is not bypassed
        mod.psql_query(f"drop table {server.table}", write=True, profile="base_rw", **conn)
    assert server.verify(f"select count(*) from {server.table}") == [(3,)]
