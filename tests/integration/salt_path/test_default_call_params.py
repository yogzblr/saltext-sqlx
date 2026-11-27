# pylint: disable=use-dict-literal
"""D16 through a REAL master and minion: the connection in the call, with NO ``sqlx:_policy`` in the minion config.

The ``salt_path`` minion has profiles and no policy, so this is the compat default end to end: calls travel
``LocalClient().cmd`` -> master -> minion job -> ``sqlx`` module -> live database, and the answers come back through the
master. Real databases from ``tests-support/env.sh``; nothing is started or stopped here.
"""

import pytest

from tests.integration.salt_path.conftest import HOST
from tests.integration.salt_path.conftest import MARIADB_PORT
from tests.integration.salt_path.conftest import MYSQL_PORT
from tests.integration.salt_path.conftest import PG_PORT
from tests.integration.salt_path.conftest import RO_PASSWORD

LITERAL_PW = RO_PASSWORD  # a literal, as Salt's own psql_query callers send


@pytest.mark.pg
def test_psql_query_with_salts_argument_names_and_a_literal_password(call):
    ret, retcode, _ = call(
        "sqlx.psql_query",
        query="select 1 as one, null as nothing",
        host=HOST,
        port=PG_PORT,
        user="ro",
        maintenance_db="sqlx_test",
        password=LITERAL_PW,
    )
    assert retcode == 0, ret
    assert ret == [{"one": "1", "nothing": None}]


@pytest.mark.pg
@pytest.mark.mysql
@pytest.mark.mariadb
@pytest.mark.parametrize(
    "dialect,port,database",
    [
        ("postgresql", PG_PORT, "sqlx_test"),
        ("mysql", MYSQL_PORT, "scratch"),
        ("mariadb", MARIADB_PORT, "scratch"),
    ],
)
def test_every_function_with_the_connection_in_the_call(call, dialect, port, database):
    conn = dict(
        dialect=dialect, host=HOST, port=port, database=database, username="ro", password=LITERAL_PW
    )
    ret, retcode, _ = call("sqlx.ping", **conn)
    assert retcode == 0 and ret["ok"] is True and ret["profile"] == "(adhoc)", ret
    ret, retcode, _ = call("sqlx.query", sql="select 1 as one", **conn)
    assert retcode == 0 and ret["rows"] == [[1]], ret
    ret, retcode, _ = call(
        "sqlx.check_row_count", sql="select 1", operator="==", expected=1, **conn
    )
    assert retcode == 0 and ret["ok"] is True, ret
    ret, retcode, _ = call(
        "sqlx.check_query_result", sql="select 1", operator="==", expected=1, **conn
    )
    assert retcode == 0 and ret["ok"] is True, ret
    ret, retcode, _ = call("sqlx.table_list", **conn)
    assert retcode == 0 and "tables" in ret, ret
    assert LITERAL_PW not in repr(ret)


@pytest.mark.pg
def test_a_write_with_an_ad_hoc_connection_commits_by_default(call, t10_tables):
    """D17: no profile, no policy: the write runs like Salt's own ``postgres.psql_query(write=True)``."""
    conn = dict(host=HOST, port=PG_PORT, database="sqlx_test", username="rw", password="rw_pw")
    insert = "insert into scratch.t10_orders values (CAST(:i AS INTEGER), :n)"
    try:
        ret, retcode, _ = call(
            "sqlx.execute",
            sql=insert,
            params={"i": 201, "n": "dry"},
            dry_run=True,
            dialect="postgresql",
            **conn,
        )
        assert retcode == 0 and (ret["committed"], ret["dry_run"]) == (False, True), ret
        ret, retcode, _ = call(
            "sqlx.execute", sql=insert, params={"i": 202, "n": "real"}, dialect="postgresql", **conn
        )
        assert retcode == 0, ret
        assert (ret["rows_affected"], ret["committed"], ret["profile"]) == (1, True, "(adhoc)")
        ret, retcode, _ = call(
            "sqlx.psql_query",
            query="insert into scratch.t10_orders values (203, 'psql')",
            host=HOST,
            port=PG_PORT,
            user="rw",
            maintenance_db="sqlx_test",
            password="rw_pw",
            write=True,
        )
        assert retcode == 0 and ret == [{"rows_affected": "1"}], ret
        ret, retcode, _ = call(
            "sqlx.query",
            sql="select id from scratch.t10_orders where id > 200 order by id",
            dialect="postgresql",
            **conn,
        )
        assert ret["rows"] == [[202], [203]], ret  # the dry run did not persist
        ret, retcode, _ = call(  # a read function never writes, even with an INSERT in it
            "sqlx.query",
            sql="insert into scratch.t10_orders values (204, 'q')",
            dialect="postgresql",
            **conn,
        )
        assert retcode == 1, ret
        with t10_tables["pg"].begin() as db:
            rows = db.exec_driver_sql(
                "select id from scratch.t10_orders where id > 200 order by id"
            )
            assert [r[0] for r in rows] == [202, 203]
    finally:
        with t10_tables["pg"].begin() as db:
            db.exec_driver_sql("DELETE FROM scratch.t10_orders WHERE id > 200")


@pytest.mark.pg
def test_an_ad_hoc_write_may_be_ddl_by_default(call):
    """D17: any single statement by default. ``rw`` has no DDL right, so the database's own answer proves it got through."""
    conn = dict(host=HOST, port=PG_PORT, database="sqlx_test", username="rw", password="rw_pw")
    ret, retcode, _ = call(
        "sqlx.execute", sql="create table scratch.t_never (a int)", dialect="postgresql", **conn
    )
    assert retcode == 1 and "[E_POLICY]" not in ret and "[E_EXECUTE_DISABLED]" not in ret, ret
    assert ret.startswith("ERROR: [E_") and "permission denied" in ret, ret


@pytest.mark.pg
def test_a_writing_profile_with_connection_fields_passes_the_execute_gate(call):
    """D16 withdrew the refusal (``allow_execute`` is the profile's): the call reaches the verb filter, which stops ``select``.

    This test asserted ``[E_EXECUTE_DISABLED]`` and was already stale on main before D17.
    """
    ret, retcode, _ = call(
        "sqlx.execute", profile="pg_rw", sql="select 1", host=HOST, password="rw_pw"
    )
    assert retcode == 1 and "[E_POLICY]" in ret and "execute_verbs" in ret, ret


@pytest.mark.pg
@pytest.mark.parametrize("host", ["169.254.169.254", "2852039166", "fe80::1"])
def test_metadata_addresses_are_refused(call, host):
    ret, retcode, _ = call(
        "sqlx.ping", dialect="postgresql", host=host, database="d", username="u", password="x"
    )
    assert retcode == 1 and "[E_POLICY]" in ret and host not in ret, ret


@pytest.mark.pg
def test_the_literal_password_is_not_echoed_in_an_error(call):
    ret, retcode, _ = call(
        "sqlx.ping",
        dialect="postgresql",
        host=HOST,
        port=PG_PORT,
        database="sqlx_test",
        username="ro",
        password="WRONG-LITERAL-PW-4471",
    )
    assert retcode == 1 and "WRONG-LITERAL-PW-4471" not in ret, ret
    assert ret.startswith("ERROR: [E_AUTH_FAILED]"), ret


@pytest.mark.pg
def test_opt_in_fields_are_refused_by_default(call):
    ret, retcode, _ = call(
        "sqlx.ping",
        dialect="postgresql",
        host=HOST,
        port=PG_PORT,
        database="sqlx_test",
        username="ro",
        password=LITERAL_PW,
        read_only=False,
    )
    assert retcode == 1 and "[E_POLICY]" in ret and "read_only" in ret, ret
