"""
Calls through ``LocalClient().cmd(..., kwarg={...})`` against a real master and minion (TEST_PLAN section 5, items 1-4).

Every assertion is on the minion's real job return (``full_return``: ``ret`` and ``retcode``), the same path the
salt-api takes minus HTTP.
"""

import pytest

from tests.integration.salt_path.conftest import BOUND_SECRET
from tests.integration.salt_path.conftest import RO_PASSWORD
from tests.integration.salt_path.conftest import SQL_MARKER

def test_minion_loads_the_module(call):
    ret, retcode, _ = call("sys.list_functions", module="sqlx")
    assert retcode == 0
    assert {"sqlx.query", "sqlx.execute", "sqlx.ping", "sqlx.table_list"} <= set(ret)


@pytest.mark.pg
def test_typed_kwargs_arrive_typed(call):
    """``params`` (an object) and ``limit`` (an int) survive the publish; a string ``limit`` would be E_OPTION_INVALID."""
    ret, retcode, _ = call(
        "sqlx.query",
        profile="pg_ro",
        sql="select id, note from t10_orders where id <= CAST(:m AS INTEGER) order by id",
        params={"m": 3},
        limit=2,
    )
    assert retcode == 0
    assert ret["profile"] == "pg_ro"
    assert ret["columns"] == ["id", "note"]
    assert ret["rows"] == [[1, "one"], [2, "two"]]
    assert ret["row_count"] == 2
    assert ret["truncated"] is True


@pytest.mark.pg
def test_string_limit_is_rejected_not_coerced(call):
    ret, retcode, _ = call("sqlx.query", profile="pg_ro", sql="select 1", limit="2")
    assert retcode == 1
    assert isinstance(ret, str)
    assert ret == "ERROR executing 'sqlx.query': [E_OPTION_INVALID] option 'limit' must be an integer >= 1"


@pytest.mark.pg
def test_pub_keys_are_dropped_and_a_typo_is_not(call):
    """Salt adds ``__pub_*`` to every job; they never trigger E_OPTION_UNKNOWN. A real typo does."""
    ret, retcode, _ = call("sqlx.ping", profile="pg_ro", __pub_t10_probe="x")
    assert retcode == 0, ret
    assert ret["ok"] is True
    ret, retcode, _ = call("sqlx.query", profile="pg_ro", sql="select 1", limt=1)
    assert retcode == 1
    # SaltInvocationError is wrapped by the minion as "ERROR executing '<fun>': <message>"
    assert ret == "ERROR executing 'sqlx.query': [E_OPTION_UNKNOWN] unknown option(s) for sqlx.query: limt"


@pytest.mark.pg
@pytest.mark.mysql
@pytest.mark.mariadb
@pytest.mark.parametrize(
    "profile, prefix, text",
    [
        ("pg_ro", "ERROR: [E_SQL] postgresql 42P01: ", 'relation "t10_nope" does not exist'),
        ("mysql_ro", "ERROR: [E_SQL] mysql 1146: ", "Table 'scratch.t10_nope' doesn't exist"),
        ("mariadb_ro", "ERROR: [E_SQL] mysql 1146: ", "Table 'scratch.t10_nope' doesn't exist"),
    ],
)
def test_bad_statement_returns_the_server_text_and_not_the_sql(call, profile, prefix, text):
    """The remote-minion form of the original problem: the real error comes back, the SQL and the bound value do not."""
    ret, retcode, _ = call(
        "sqlx.query",
        profile=profile,
        sql=f"select id from t10_nope where note = :s or 'a' = '{SQL_MARKER}'",
        params={"s": BOUND_SECRET},
    )
    assert retcode == 1
    assert isinstance(ret, str)
    assert ret == f"{prefix}{text}"
    for leaked in (SQL_MARKER, BOUND_SECRET, "select id from", "[SQL:", "[parameters:", RO_PASSWORD):
        assert leaked not in ret


@pytest.mark.pg
def test_syntax_error_on_postgresql(call):
    ret, retcode, _ = call("sqlx.query", profile="pg_ro", sql="selec 1")
    assert retcode == 1
    assert ret.startswith("ERROR: [E_SQL] postgresql 42601: ")
    assert "syntax error" in ret


@pytest.mark.pg
@pytest.mark.mysql
@pytest.mark.mariadb
@pytest.mark.parametrize("profile", ["pg_ro", "mysql_ro", "mariadb_ro"])
def test_ping_on_every_live_database(call, profile):
    ret, retcode, _ = call("sqlx.ping", profile=profile)
    assert retcode == 0
    assert ret["ok"] is True
    assert ret["profile"] == profile


@pytest.mark.pg
def test_unknown_profile_does_not_disclose_other_names(call):
    ret, retcode, _ = call("sqlx.ping", profile="t10_no_such_profile")
    assert retcode == 1
    assert ret == "ERROR executing 'sqlx.ping': [E_PROFILE_UNKNOWN] profile is not configured"
    assert "pg_ro" not in ret
    assert "mysql_ro" not in ret


@pytest.mark.pg
def test_sdb_password_resolution(call):
    """``password: sdb://t10_sdb/<key>`` resolves through a throwaway yaml sdb profile on the minion."""
    ret, retcode, _ = call(
        "sqlx.query", profile="pg_sdb", sql="select CAST(:v AS INTEGER) as v", params={"v": 7}
    )
    assert retcode == 0, ret
    assert ret["rows"] == [[7]]


@pytest.mark.pg
@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT in sqlx_mod._run: __salt__['config.get']('sqlx') resolves sdb:// itself, and a key missing from the "
        "yaml sdb becomes password=None, which profiles.load_profile treats as 'no password'. The module then connects "
        "without a password and returns '[E_INTERNAL] postgresql: OperationalError' instead of '[E_SECRET] ...'."
    ),
)
def test_sdb_reference_that_does_not_resolve_is_e_secret(call):
    ret, retcode, _ = call("sqlx.ping", profile="pg_sdb_missing")
    assert retcode == 1
    assert ret.startswith("ERROR: [E_SECRET] ")
    assert "sdb://t10_sdb/t10_no_such_key" in ret
    assert RO_PASSWORD not in ret


@pytest.mark.pg
@pytest.mark.mysql
@pytest.mark.mariadb
@pytest.mark.parametrize("profile", ["pg_badpw", "mysql_badpw", "mariadb_badpw"])
def test_wrong_password_is_e_auth_failed(call, profile):
    ret, retcode, _ = call("sqlx.ping", profile=profile)
    assert retcode == 1
    assert ret.startswith("ERROR: [E_AUTH_FAILED] ")
    assert "t10_wrong_pw_c94d" not in ret


@pytest.mark.pg
def test_execute_is_disabled_by_default(call):
    ret, retcode, _ = call(
        "sqlx.execute", profile="pg_ro", sql="insert into t10_orders values (100, 'x')"
    )
    assert retcode == 1
    assert ret.startswith("ERROR: [E_EXECUTE_DISABLED] ")


@pytest.mark.pg
def test_execute_on_an_allow_execute_profile_commits_and_dry_run_does_not(call, t10_tables):
    ret, retcode, _ = call(
        "sqlx.execute",
        profile="pg_rw",
        sql="insert into t10_orders values (CAST(:i AS INTEGER), :n)",
        params={"i": 101, "n": "dry"},
        dry_run=True,
    )
    assert retcode == 0, ret
    assert (ret["rows_affected"], ret["committed"], ret["dry_run"]) == (1, False, True)
    ret, retcode, _ = call(
        "sqlx.execute",
        profile="pg_rw",
        sql="insert into t10_orders values (CAST(:i AS INTEGER), :n)",
        params={"i": 102, "n": "real"},
    )
    assert retcode == 0, ret
    assert (ret["rows_affected"], ret["committed"]) == (1, True)
    try:
        ret, retcode, _ = call(
            "sqlx.query",
            profile="pg_ro",
            sql="select id from t10_orders where id > 100 order by id",
        )
        assert ret["rows"] == [[102]]
    finally:
        with t10_tables["pg"].begin() as conn:
            conn.exec_driver_sql("DELETE FROM scratch.t10_orders WHERE id > 100")


@pytest.mark.pg
def test_check_query_result_operator_must_be_yaml_quoted_on_the_cli(minion, salt_cli):
    """The CLI form: ``operator='"=="'`` reaches the module as the string ``==`` (a bare ``==`` is parsed by YAML)."""
    ret = salt_cli.run(
        "sqlx.check_query_result",
        "profile=pg_ro",
        "sql=select count(*) from t10_orders",
        'operator="=="',
        "expected=3",
        "--out=json",
        minion_tgt=minion.id,
    )
    assert ret.returncode == 0, ret.stdout + ret.stderr
    assert ret.data["ok"] is True
    assert ret.data["operator"] == "=="


@pytest.mark.pg
@pytest.mark.parametrize("bare", ["operator===", "operator=>"])
def test_unquoted_operator_on_the_cli_fails(minion, salt_cli, bare):
    """YAML eats a bare ``==`` (the CLI splits ``operator===`` at the first ``=``) and ``>`` (a block scalar)."""
    ret = salt_cli.run(
        "sqlx.check_query_result",
        "profile=pg_ro",
        "sql=select count(*) from t10_orders",
        bare,
        "expected=3",
        "--out=json",
        minion_tgt=minion.id,
    )
    assert ret.returncode == 1
    assert "[E_OPTION_INVALID]" in ret.stdout or "Passed invalid arguments" in ret.stdout
