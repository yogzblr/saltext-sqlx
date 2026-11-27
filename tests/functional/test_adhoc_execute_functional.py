"""D17 against REAL servers: ``execute`` and ``psql_query write=True`` with the connection in the call and no profile.

No ``sqlx:_policy`` and no profile unless a test sets one, so every call is ad-hoc and the write permission is the default
(on). PostgreSQL 54329 (pg8000 and psycopg), MySQL 33061, MariaDB 33062; the ``rw`` account for DML, the admin account for
the DDL tests (``rw`` has no DDL on purpose). Oracle and SQL Server cannot run here (no servers): unit and security only.
"""

# pylint: disable=redefined-outer-name,unused-argument
import uuid

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.functional.test_runner_functional import ADMIN_PW
from tests.functional.test_runner_functional import DIALECTS
from tests.functional.test_runner_functional import HOST
from tests.functional.test_runner_functional import _reachable

pytestmark = [pytest.mark.pg, pytest.mark.mysql, pytest.mark.mariadb]


@pytest.fixture(scope="module", params=DIALECTS)
def server(request):
    srv, reason = _reachable(request.param)
    if reason:
        pytest.skip(reason)
    srv.table = f"scratch.t17_{uuid.uuid4().hex[:10]}"
    srv.admin_run(f"CREATE TABLE {srv.table} (k varchar(20) PRIMARY KEY, n integer)")
    srv.admin_run(f"INSERT INTO {srv.table} (k, n) VALUES ('a', 1), ('b', 2)")
    if srv.key == "postgresql":
        srv.admin_run(f"GRANT SELECT ON {srv.table} TO ro")
        srv.admin_run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {srv.table} TO rw")
    try:
        yield srv
    finally:
        srv.admin_run(f"DROP TABLE IF EXISTS {srv.table}")


@pytest.fixture
def cfg(monkeypatch, server):
    config = {}
    server.admin_run(f"DELETE FROM {server.table}")
    server.admin_run(f"INSERT INTO {server.table} (k, n) VALUES ('a', 1), ('b', 2)")
    monkeypatch.setattr(
        mod,
        "__salt__",
        {
            "config.get": lambda key, default=None: config if key == "sqlx" else default,
            "sdb.get": {"sdb://vault/rw": "rw_pw"}.__getitem__,
        },
        raising=False,
    )
    return config


def conn(server, **extra):
    base = {
        "dialect": server.dialect,
        "host": HOST,
        "port": server.cfg["port"],
        "database": server.cfg["database"],
        "username": "rw",
        "password": "rw_pw",
    }
    base.update(extra)
    return base


def count(server, where="1=1"):
    return server.verify(f"select count(*) from {server.table} where {where}")[0][0]


def test_insert_update_delete_commit_and_a_second_connection_sees_them(cfg, server):
    out = mod.execute(sql=f"insert into {server.table} (k, n) values ('c', 3)", **conn(server))
    assert (out["rows_affected"], out["committed"], out["profile"]) == (1, True, "(adhoc)")
    assert server.verify(f"select n from {server.table} where k = 'c'") == [(3,)]
    out = mod.execute(
        sql=f"update {server.table} set n = n + 10 where k in ('a','c')", **conn(server)
    )
    assert out["rows_affected"] == 2 and out["committed"] is True
    assert server.verify(f"select k, n from {server.table} order by k") == [
        ("a", 11),
        ("b", 2),
        ("c", 13),
    ]
    out = mod.execute(sql=f"delete from {server.table} where k = 'c'", **conn(server))
    assert out["rows_affected"] == 1
    assert count(server) == 2


def test_bind_parameters_work(cfg, server):
    mod.execute(
        sql=f"insert into {server.table} (k, n) values (:k, :n)",
        params={"k": "p", "n": 5},
        **conn(server),
    )
    assert server.verify(f"select n from {server.table} where k = 'p'") == [(5,)]


def test_dry_run_reports_the_rows_and_rolls_back(cfg, server):
    out = mod.execute(
        sql=f"insert into {server.table} (k, n) values ('d', 4)", dry_run=True, **conn(server)
    )
    assert (out["rows_affected"], out["committed"], out["dry_run"]) == (1, False, True)
    assert count(server, "k = 'd'") == 0


def test_sdb_reference_and_connection_object(cfg, server):
    out = mod.execute(
        sql=f"insert into {server.table} (k, n) values ('s', 6)",
        connection=conn(server, password="sdb://vault/rw"),
    )
    assert out["committed"] is True and count(server, "k = 's'") == 1


def test_wrong_password_gives_the_real_error_and_nothing_is_written(cfg, server):
    with pytest.raises(CommandExecutionError) as exc:
        mod.execute(
            sql=f"insert into {server.table} (k, n) values ('w', 1)",
            **conn(server, password="WRONG-literal-pw-17"),
        )
    assert str(exc.value).startswith("[E_AUTH_FAILED]") and "WRONG-literal-pw-17" not in str(
        exc.value
    )
    assert count(server, "k = 'w'") == 0


def test_a_real_database_error_comes_back_and_rolls_back(cfg, server):
    with pytest.raises(CommandExecutionError) as exc:
        mod.execute(sql=f"insert into {server.table} (k, n) values ('a', 9)", **conn(server))
    assert str(exc.value).startswith("[E_CONSTRAINT]")
    assert server.verify(f"select n from {server.table} where k = 'a'") == [(1,)]


def test_multi_statement_is_refused(cfg, server):
    sql = f"insert into {server.table} (k, n) values ('m', 1); delete from {server.table}"
    drivers = [None, "pg8000", "psycopg"] if server.key == "postgresql" else [None]
    for driver in drivers:
        extra = {"driver": driver} if driver else {}
        with pytest.raises((CommandExecutionError, SaltInvocationError)):
            mod.execute(sql=sql, **conn(server, **extra))
        assert count(server) == 2 and count(server, "k = 'm'") == 0, driver


def test_an_ad_hoc_query_with_an_insert_writes_nothing(cfg, server):
    sql = f"insert into {server.table} (k, n) values ('q', 1)"
    try:
        mod.query(sql=sql, **conn(server))
    except (CommandExecutionError, SaltInvocationError):
        pass
    assert count(server, "k = 'q'") == 0


def test_psql_query_write_with_salts_names(cfg, server):
    if server.key != "postgresql":
        pytest.skip("PostgreSQL only")
    args = {
        "user": "rw",
        "host": HOST,
        "port": server.cfg["port"],
        "maintenance_db": server.cfg["database"],
        "password": "rw_pw",
    }
    out = mod.psql_query(f"insert into {server.table} (k, n) values ('x', 7);", write=True, **args)
    assert out == [{"rows_affected": "1"}] and count(server, "k = 'x'") == 1
    out = mod.psql_query(
        f"delete from {server.table} where k = 'x'", write=True, dry_run=True, **args
    )
    assert out == [{"rows_affected": "1"}] and count(server, "k = 'x'") == 1  # rolled back
    assert mod.psql_query(f"select count(*) as c from {server.table}", **args) == [{"c": "3"}]


def test_the_default_verbs_refuse_ddl_and_the_operator_can_allow_it(cfg, server):
    admin = conn(server, username=server.cfg["admin"], password=ADMIN_PW)
    name = f"scratch.t17_ddl_{uuid.uuid4().hex[:6]}"
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*execute_verbs"):
        mod.execute(sql=f"create table {name} (a integer)", **admin)
    cfg["_policy"] = {"adhoc_execute_verbs": ["INSERT", "create", "DROP"]}
    try:
        out = mod.execute(sql=f"create table {name} (a integer)", **admin)
        assert out["committed"] is True
        mod.execute(sql=f"insert into {name} values (1)", **admin)
        assert server.verify(f"select a from {name}") == [(1,)]
    finally:
        server.admin_run(f"drop table if exists {name}")


def test_the_caps_still_clamp_for_an_ad_hoc_write(cfg, server):
    out = mod.execute(
        sql=f"update {server.table} set n = n", timeout_ms=10**9, **conn(server)
    )  # clamped silently to the hard default, not an error
    assert out["committed"] is True


@pytest.mark.parametrize("host", ["169.254.169.254", "fe80::1"])
def test_metadata_targets_are_refused_for_a_write(cfg, server, host):
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.execute(sql="insert into t values (1)", **conn(server, host=host))


def test_opt_out_gives_the_old_refusal(cfg, server):
    cfg["_policy"] = {"allow_adhoc_execute": False}
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.execute(sql=f"insert into {server.table} (k, n) values ('o', 1)", **conn(server))
    assert count(server, "k = 'o'") == 0


def test_opt_out_with_the_other_policies(cfg, server):
    cfg["_policy"] = {"allow_adhoc_connection": False}
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allow_adhoc_connection"):
        mod.execute(sql=f"insert into {server.table} (k, n) values ('o', 1)", **conn(server))
    cfg["_policy"] = {"allow_call_params": []}
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
        mod.execute(sql=f"insert into {server.table} (k, n) values ('o', 1)", **conn(server))
    assert count(server, "k = 'o'") == 0
