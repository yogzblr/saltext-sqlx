"""Connections given entirely in the call (SPEC section 3.2, ad-hoc), on ``minion-adhoc``: it has no sqlx profiles.

Its policy (``salt/minion-adhoc.d/10-adhoc.conf``) allows every call-time field and ``allow_adhoc_connection``, with
the five test databases as the only allowed hosts. The user guide's call-time examples are taken from these runs.
"""

import pytest
from conftest import DBS
from conftest import PATHS
from conftest import assert_error
from conftest import assert_ok
from dbs import SCHEMA
from dbs import sql

M = "minion-adhoc"
PW = "sdb://e2e_sdb/ro_pw"

#: what a caller sends instead of a profile name (the values of the `<db>_ro` profiles on the other minions)
CONN = {
    "pg": {"dialect": "postgresql", "host": "pg", "port": 5432, "database": "sakila"},
    "mysql": {"dialect": "mysql", "host": "mysql", "port": 3306, "database": "sakila"},
    "mariadb": {"dialect": "mariadb", "host": "mariadb", "port": 3306, "database": "sakila"},
    "oracle": {"dialect": "oracle", "host": "oracle", "port": 1521, "database": "SAKILA"},
    "mssql": {"dialect": "mssql", "host": "mssql", "port": 1433, "database": "sakila"},
}
DIALECT = {
    "pg": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "mssql": "mssql",
}


def conn(db, **extra):
    return dict(CONN[db], username="ro", password=PW, **extra)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("db", DBS)
def test_adhoc_ping_with_flat_keywords(salt, db, path):
    out = assert_ok(salt.run(path, "sqlx.ping", minion=M, **conn(db)))
    assert out["ok"] is True and out["dialect"] == DIALECT[db] and out["profile"] == "(adhoc)"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("db", DBS)
def test_adhoc_query_with_a_connection_object(salt, db, path):
    res = salt.run(path, "sqlx.query", minion=M, sql=sql(db, "count_film"), connection=conn(db))
    out = assert_ok(res)
    assert out["rows"] == [[1000]] and out["profile"] == "(adhoc)"


@pytest.mark.parametrize("db", DBS)
def test_adhoc_port_defaults_to_the_dialect_port(salt, db):
    without_port = {k: v for k, v in conn(db).items() if k != "port"}
    assert assert_ok(salt.cli("sqlx.ping", minion=M, **without_port))["ok"] is True


@pytest.mark.parametrize("db", DBS)
def test_adhoc_bind_parameters_and_lowered_limits(salt, db):
    res = salt.api(
        "sqlx.query",
        minion=M,
        sql=sql(db, "films"),
        limit=3,
        statement_timeout_ms=5000,
        read_only=True,
        **conn(db),
    )
    out = assert_ok(res)
    assert out["rows"] == [[1], [2], [3]] and out["truncated"] is True
    res = salt.cli("sqlx.query", minion=M, sql=sql(db, "cast"), params={"x": 41}, **conn(db))
    assert assert_ok(res)["rows"] == [[42]]


@pytest.mark.parametrize("db", DBS)
def test_adhoc_checks_and_table_list(salt, db):
    res = salt.cli(
        "sqlx.check_query_result",
        minion=M,
        sql=sql(db, "count_film"),
        operator="==",
        expected=1000,
        **conn(db),
    )
    assert assert_ok(res)["ok"] is True
    res = salt.api("sqlx.table_list", minion=M, schema=SCHEMA[db], **conn(db))
    assert "film" in assert_ok(res)["tables"]


def test_adhoc_psql_query(salt):
    # postgres.psql_query has no dialect argument; without a profile the call names it (SPEC section 4.7)
    res = salt.cli(
        "sqlx.psql_query",
        "select 1 as one, null as nothing",
        minion=M,
        dialect="postgresql",
        user="ro",
        host="pg",
        port=5432,
        maintenance_db="sakila",
        password=PW,
    )
    assert assert_ok(res) == [{"one": "1", "nothing": None}]


@pytest.mark.parametrize("db", DBS)
def test_adhoc_execute_is_refused(salt, db):
    # no profile, so no allow_execute: a call cannot grant it (SPEC section 3.2)
    res = salt.api("sqlx.execute", minion=M, sql=sql(db, "ro_write"), **conn(db))
    assert_error(res, "E_EXECUTE_DISABLED")


@pytest.mark.parametrize(
    "key,value",
    [("row_limit", 9999), ("allow_execute", True), ("error_detail", True), ("_policy", {})],
)
def test_adhoc_cannot_set_profile_only_keys(salt, key, value):
    assert_error(salt.cli("sqlx.ping", minion=M, **conn("pg"), **{key: value}), "E_OPTION_UNKNOWN")


def test_adhoc_policy_refusals(salt):
    assert_error(
        salt.cli("sqlx.ping", minion=M, **dict(conn("pg"), host="evil.example")), "E_POLICY"
    )
    assert_error(salt.cli("sqlx.ping", minion=M, **dict(conn("pg"), port=6543)), "E_POLICY")
    assert_error(
        salt.cli("sqlx.ping", minion=M, **dict(conn("pg"), password="literal")), "E_POLICY"
    )
    assert_error(salt.cli("sqlx.ping", minion=M, **dict(conn("pg"), read_only=False)), "E_POLICY")


def test_adhoc_needs_dialect_host_database_and_username(salt):
    partial = {k: v for k, v in conn("pg").items() if k != "username"}
    # without all four the call is not ad-hoc, so it needs a profile (and this minion has none)
    assert_error(
        salt.cli("sqlx.ping", minion=M, **partial), "E_OPTION_INVALID", "'profile' is required"
    )


def test_eauth_pins_the_host_of_an_adhoc_call(salt):
    # adhoc_ops may reach only pg and mysql (salt/master.d/e2e.conf): the rule matches host and dialect, not profile
    out = assert_ok(salt.api("sqlx.ping", minion=M, user="adhoc_ops", **conn("pg")))
    assert out["ok"] is True
    for refused in (
        conn("mariadb"),  # host not in the rule
        dict(conn("pg"), dialect="mssql"),  # dialect not in the rule
        {k: v for k, v in conn("pg").items() if k != "host"},  # a kwarg the rule names is missing
    ):
        res = salt.api("sqlx.ping", minion=M, user="adhoc_ops", **refused)
        assert not res.ok and res.retcode == 401, res
    res = salt.api("sqlx.query", minion=M, user="adhoc_ops", sql="select 1", connection=conn("pg"))
    assert not res.ok and res.retcode == 401, res  # the fields inside connection= are not matched
