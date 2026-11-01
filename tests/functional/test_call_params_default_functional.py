"""D16 against REAL servers: the connection in the call with NO ``sqlx:_policy`` (PostgreSQL 54329, MySQL 33061, MariaDB 33062).

The minion config has no policy and (unless a test adds one) no profile at all, so every call is ad-hoc. The functions are
the real ones; only ``__salt__`` is injected, as in the other functional module tests. Oracle and SQL Server cannot run here
(no servers); their dialect handling is covered by the unit and security suites.
"""

# pylint: disable=redefined-outer-name,unused-argument
import uuid

import pytest
from salt.exceptions import CommandExecutionError
from salt.exceptions import SaltInvocationError

import saltext.sqlx.modules.sqlx_mod as mod
from tests.functional.test_call_params_functional import who
from tests.functional.test_runner_functional import DIALECTS
from tests.functional.test_runner_functional import HOST
from tests.functional.test_runner_functional import _reachable

pytestmark = [pytest.mark.pg, pytest.mark.mysql, pytest.mark.mariadb]

SDB = {"sdb://vault/ro": "ro_pw"}


@pytest.fixture(scope="module", params=DIALECTS)
def server(request):
    srv, reason = _reachable(request.param)
    if reason:
        pytest.skip(reason)
    srv.table = f"scratch.t16_{uuid.uuid4().hex[:10]}"
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
def cfg(monkeypatch):
    """No ``_policy``, no profiles."""
    config = {}
    monkeypatch.setattr(
        mod,
        "__salt__",
        {
            "config.get": lambda key, default=None: config if key == "sqlx" else default,
            "sdb.get": SDB.__getitem__,
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
        "username": "ro",
        "password": "ro_pw",  # a literal
    }
    base.update(extra)
    return base


def test_every_function_with_a_literal_password(cfg, server):
    kw = conn(server)
    assert mod.ping(**kw)["ok"] is True
    out = mod.query(sql=who(server), **kw)
    assert out["profile"] == "(adhoc)" and out["rows"][0][0].split("@")[0] == "ro"
    sql = f"select count(*) from {server.table}"
    assert mod.check_query_result(sql=sql, operator="==", expected=2, **kw)["ok"]
    assert mod.check_row_count(
        sql=f"select * from {server.table}", operator="==", expected=2, **kw
    )["ok"]
    tables = mod.table_list(schema="scratch", **kw)["tables"]
    assert server.table.split(".")[1] in tables


def test_sdb_reference_and_connection_object(cfg, server):
    out = mod.query(sql=who(server), connection=conn(server, password="sdb://vault/ro"))
    assert out["rows"][0][0].split("@")[0] == "ro"


def test_mariadb_is_accepted_as_a_dialect_value(cfg, server):
    if server.dialect != "mysql":
        pytest.skip("MySQL family only")
    assert mod.ping(**conn(server, dialect="mariadb"))["dialect"] == "mysql"


def test_wrong_literal_password_is_the_real_auth_error_without_the_password(cfg, server):
    with pytest.raises(CommandExecutionError) as exc:
        mod.ping(**conn(server, password="WRONG-literal-pw-91"))
    assert str(exc.value).startswith("[E_AUTH_FAILED]") and "WRONG-literal-pw-91" not in str(
        exc.value
    )


def test_psql_query_with_salts_names(cfg, server):
    if server.key != "postgresql":
        with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_INVALID\].*postgresql"):
            mod.psql_query(
                "select 1",
                user="ro",
                host=HOST,
                port=server.cfg["port"],
                maintenance_db="scratch",
                password="ro_pw",
                dialect=server.dialect,
            )
        return
    out = mod.psql_query(
        "select 1 as one, null as nothing, true as flag;",
        user="ro",
        host=HOST,
        port=server.cfg["port"],
        maintenance_db=server.cfg["database"],
        password="ro_pw",
    )
    assert out == [{"one": "1", "nothing": None, "flag": "t"}]


def test_an_ad_hoc_connection_cannot_write_even_with_the_rw_account(cfg, server):
    kw = conn(server, username="rw", password="rw_pw")
    sql = f"insert into {server.table} (k, n) values ('zz', 9)"
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.execute(sql=sql, **kw)
    if server.key == "postgresql":
        with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
            mod.psql_query(
                sql,
                user="rw",
                host=HOST,
                port=server.cfg["port"],
                maintenance_db=server.cfg["database"],
                password="rw_pw",
                write=True,
            )
    assert server.admin_run(f"select count(*) from {server.table} where k = 'zz'")[0][0] == 0


def test_a_writing_profile_writes_with_connection_fields_in_the_call(cfg, server):
    """As in Salt: the call may name the connection; ``allow_execute`` is the profile's, not the call's."""
    cfg["profiles"] = {
        "w": {
            "dialect": server.dialect,
            "host": HOST,
            "port": server.cfg["port"],
            "database": server.cfg["database"],
            "username": "rw",
            "password": "rw_pw",
            "allow_execute": True,
            "read_only": False,
        }
    }
    sql = f"insert into {server.table} (k, n) values ('yy', 8)"
    out = mod.execute(profile="w", sql=sql, database=server.cfg["database"])
    assert out["rows_affected"] == 1 and out["committed"] is True
    assert server.admin_run(f"select count(*) from {server.table} where k = 'yy'")[0][0] == 1
    server.admin_run(f"delete from {server.table} where k = 'yy'")
    with pytest.raises(CommandExecutionError, match=r"^\[E_EXECUTE_DISABLED\]"):
        mod.execute(sql=sql, **conn(server))  # no profile: nothing says writes are allowed
    assert server.admin_run(f"select count(*) from {server.table} where k = 'yy'")[0][0] == 0


def test_caps_stay_the_hard_defaults(cfg, server):
    out = mod.query(
        sql=f"select * from {server.table}",
        limit=1,
        timeout_ms=10**9,
        max_bytes=10**12,
        **conn(server),
    )
    assert out["row_count"] == 1 and out["truncated"] is True


@pytest.mark.parametrize("host", ["169.254.169.254", "fe80::1"])
def test_metadata_target_is_refused_without_a_connection(cfg, server, host):
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\]"):
        mod.ping(**conn(server, host=host))


def test_strict_config_gives_the_old_rejections(cfg, server):
    cfg["_policy"] = {"allow_call_params": []}
    with pytest.raises(SaltInvocationError, match=r"^\[E_OPTION_UNKNOWN\]"):
        mod.ping(**conn(server))
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allow_call_params"):
        mod.psql_query("select 1", user="ro", host=HOST, maintenance_db="x", password="p")


def test_allowed_hosts_narrow_the_default(cfg, server):
    cfg["_policy"] = {"allowed_hosts": ["10.9.0.0/16"]}
    with pytest.raises(SaltInvocationError, match=r"^\[E_POLICY\].*allowed_hosts"):
        mod.ping(**conn(server))
    cfg["_policy"] = {"allowed_hosts": ["127.0.0.1"]}
    assert mod.ping(**conn(server))["ok"] is True
